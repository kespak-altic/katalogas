import pytest
import requests_mock
from django.core import mail
from django.urls import reverse
from django_webtest import DjangoTestApp

from vitrina.orgs.factories import OrganizationFactory, RepresentativeFactory
from vitrina.orgs.models import Organization
from vitrina.projects.factories import ProjectFactory, UseCaseClientFactory, UseCaseClientScopeFactory
from vitrina.smart_contracts import AgreementStatuses
from vitrina.smart_contracts.factories import AgreementFactory
from vitrina.smart_contracts.models import Agreement, AgreementScope
from vitrina.users.factories import UserFactory
from vitrina.users.models import User

pytestmark = pytest.mark.django_db

REJECTABLE = [
    AgreementStatuses.CREATED,
    AgreementStatuses.SUBMITTED,
    AgreementStatuses.APPROVED,
    AgreementStatuses.FORMED,
    AgreementStatuses.INITIATED,
]


@pytest.fixture
def oauth(settings):
    settings.OAUTH_SERVER_TOKEN_URL = "https://oauth.test/token"
    settings.OAUTH_SERVER_CLIENTS_URL = "https://oauth.test/clients"
    with requests_mock.Mocker() as mocker:
        mocker.post(settings.OAUTH_SERVER_TOKEN_URL, json={"access_token": "token"})
        yield mocker


def _agreement(status: str, **kwargs) -> Agreement:
    assignee, assigner = OrganizationFactory.create_batch(2)
    project = ProjectFactory(organization=assignee)
    return AgreementFactory(project=project, assignee=assignee, assigner=assigner, status=status, **kwargs)


def _representative(organization: Organization, can_make_agreements: bool = True) -> User:
    user = UserFactory(organization=organization, is_viisp_login=True, viisp_company_code=organization.company_code)
    RepresentativeFactory(user=user, content_object=organization, can_make_agreements=can_make_agreements)
    return user


def _scopes(agreement: Agreement, *scopes: str) -> None:
    for scope in scopes:
        resource, action = scope.rsplit("/:", 1)
        AgreementScope.objects.create(agreement=agreement, scope=scope, resource=resource, action=action)


def _url(action: str, agreement: Agreement) -> str:
    return reverse(f"project-agreement-{action}", args=[agreement.project.pk, agreement.pk])


@pytest.mark.parametrize("status", REJECTABLE)
def test_recipient_rejects_agreement_with_reason(app: DjangoTestApp, status: str):
    agreement = _agreement(status)
    app.set_user(_representative(agreement.assignee))

    resp = app.post(_url("reject", agreement), {"status_reason": "Duomenys nebereikalingi"})

    assert resp.status_code == 302
    agreement.refresh_from_db()
    assert (agreement.status, agreement.status_reason) == (AgreementStatuses.REJECTED, "Duomenys nebereikalingi")


def test_provider_rejects_agreement_on_organization_page(app: DjangoTestApp):
    agreement = _agreement(AgreementStatuses.SUBMITTED)
    app.set_user(_representative(agreement.assigner))

    app.post(reverse("organization-agreement-reject", args=[agreement.assigner.pk, agreement.pk]), {})

    agreement.refresh_from_db()
    assert agreement.status == AgreementStatuses.REJECTED


def test_staff_rejects_agreement_without_representing_a_party(app: DjangoTestApp):
    agreement = _agreement(AgreementStatuses.FORMED)
    app.set_user(UserFactory(is_staff=True))

    app.post(_url("reject", agreement), {})

    agreement.refresh_from_db()
    assert agreement.status == AgreementStatuses.REJECTED


@pytest.mark.parametrize(
    "status",
    [AgreementStatuses.SIGNED, AgreementStatuses.ACTIVE, AgreementStatuses.TERMINATED, AgreementStatuses.REJECTED],
)
def test_signed_or_ended_agreement_cannot_be_rejected(app: DjangoTestApp, status: str):
    agreement = _agreement(status)
    app.set_user(_representative(agreement.assignee))

    app.post(_url("reject", agreement), {})

    agreement.refresh_from_db()
    assert agreement.status == status


@pytest.mark.parametrize("action", ["reject", "terminate"])
def test_representative_without_agreement_rights_cannot_end_agreement(app: DjangoTestApp, action: str):
    agreement = _agreement(AgreementStatuses.INITIATED if action == "reject" else AgreementStatuses.SIGNED)
    app.set_user(_representative(agreement.assignee, can_make_agreements=False))

    resp = app.post(_url(action, agreement), {"status_reason": "Priežastis"}, expect_errors=True)

    assert resp.status_code == 403
    agreement.refresh_from_db()
    assert agreement.status != AgreementStatuses.REJECTED and agreement.status != AgreementStatuses.TERMINATED


def test_terminating_revokes_scopes_no_other_active_agreement_grants(app: DjangoTestApp, oauth):
    agreement = _agreement(AgreementStatuses.SIGNED, is_agent_sync_enabled=True)
    _scopes(agreement, "uapi:/datasets/gov/a/:getall", "uapi:/datasets/gov/b/:getall")
    other = AgreementFactory(
        project=agreement.project, assignee=agreement.assignee, assigner=OrganizationFactory(), status="ACTIVE"
    )
    _scopes(other, "uapi:/datasets/gov/b/:getall")
    client = UseCaseClientFactory(use_case=agreement.project)
    for scope in ("uapi:/datasets/gov/a/:getall", "uapi:/datasets/gov/b/:getall"):
        UseCaseClientScopeFactory(use_case_client=client, scope=scope)
    update = oauth.patch(f"https://oauth.test/clients/{client.client_id}", json={})
    app.set_user(_representative(agreement.assignee))

    app.post(_url("terminate", agreement), {"status_reason": "Projektas baigtas"})

    agreement.refresh_from_db()
    assert (agreement.status, agreement.status_reason) == (AgreementStatuses.TERMINATED, "Projektas baigtas")
    assert not agreement.is_agent_sync_enabled
    assert list(client.scopes.values_list("scope", flat=True)) == ["uapi:/datasets/gov/b/:getall"]
    assert update.last_request.json() == {"scopes": ["uapi:/datasets/gov/b/:getall"]}
    assert sorted(message.to[0] for message in mail.outbox) == sorted(
        [agreement.assignee.email, agreement.assigner.email]
    )


def test_termination_stays_in_progress_until_identity_server_confirms(app: DjangoTestApp, oauth):
    agreement = _agreement(AgreementStatuses.ACTIVE, is_agent_sync_enabled=True)
    _scopes(agreement, "uapi:/datasets/gov/a/:getall")
    client = UseCaseClientFactory(use_case=agreement.project)
    UseCaseClientScopeFactory(use_case_client=client, scope="uapi:/datasets/gov/a/:getall")
    url = f"https://oauth.test/clients/{client.client_id}"
    oauth.patch(url, status_code=503)
    app.set_user(_representative(agreement.assigner))

    app.post(_url("terminate", agreement), {"status_reason": "Pažeistos sąlygos"})

    agreement.refresh_from_db()
    assert agreement.status == AgreementStatuses.TERMINATING
    assert not agreement.is_agent_sync_enabled
    assert not client.scopes.exists()
    assert mail.outbox == []

    oauth.patch(url, json={})
    app.post(_url("terminate", agreement), {"status_reason": "Pažeistos sąlygos"})

    agreement.refresh_from_db()
    assert agreement.status == AgreementStatuses.TERMINATED
    assert len(mail.outbox) == 2


def test_staff_terminates_agreement_on_organization_page(app: DjangoTestApp, oauth):
    agreement = _agreement(AgreementStatuses.SIGNED)
    app.set_user(UserFactory(is_staff=True))

    app.post(
        reverse("organization-agreement-terminate", args=[agreement.assigner.pk, agreement.pk]),
        {"status_reason": "Sutartis nebegalioja"},
    )

    agreement.refresh_from_db()
    assert agreement.status == AgreementStatuses.TERMINATED


@pytest.mark.parametrize("status", REJECTABLE)
def test_unsigned_agreement_cannot_be_terminated(app: DjangoTestApp, oauth, status: str):
    agreement = _agreement(status)
    app.set_user(_representative(agreement.assignee))

    app.post(_url("terminate", agreement), {"status_reason": "Priežastis"})

    agreement.refresh_from_db()
    assert agreement.status == status


def test_termination_needs_a_reason(app: DjangoTestApp, oauth):
    agreement = _agreement(AgreementStatuses.SIGNED)
    app.set_user(_representative(agreement.assignee))

    resp = app.post(_url("terminate", agreement), {"status_reason": ""})

    assert resp.status_code == 200
    assert "status_reason" in resp.context["form"].errors
    agreement.refresh_from_db()
    assert agreement.status == AgreementStatuses.SIGNED


@pytest.mark.parametrize(
    "status, action, shown",
    [
        (AgreementStatuses.INITIATED, "reject", True),
        (AgreementStatuses.SIGNED, "terminate", True),
        (AgreementStatuses.TERMINATING, "terminate", True),
        (AgreementStatuses.SIGNED, "reject", False),
        (AgreementStatuses.INITIATED, "terminate", False),
    ],
)
def test_detail_page_shows_the_action_the_status_allows(app: DjangoTestApp, status: str, action: str, shown: bool):
    agreement = _agreement(status)
    app.set_user(_representative(agreement.assignee))

    page = app.get(reverse("project-agreement-detail", args=[agreement.project.pk, agreement.pk]))

    assert (_url(action, agreement) in page) is shown


def test_detail_page_hides_actions_from_user_without_agreement_rights(app: DjangoTestApp):
    agreement = _agreement(AgreementStatuses.SIGNED)
    app.set_user(_representative(agreement.assignee, can_make_agreements=False))

    page = app.get(reverse("project-agreement-detail", args=[agreement.project.pk, agreement.pk]))

    assert _url("terminate", agreement) not in page
