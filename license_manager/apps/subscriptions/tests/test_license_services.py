from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError

from license_manager.apps.subscriptions import constants
from license_manager.apps.subscriptions.models import FeaturePermission
from license_manager.apps.subscriptions.services.licenses import (
    DEFAULT_CATALOG_FEATURE_SLUG,
    assign_license,
    create_licenses_for_purchase,
    expire_time_limited_licenses,
)
from license_manager.apps.subscriptions.tests.factories import (
    LicenseFactory,
    SubscriptionPlanFactory,
)
from license_manager.apps.subscriptions.utils import localized_utcnow


@pytest.mark.django_db
def test_create_licenses_for_purchase_copies_plan_permissions():
    plan = SubscriptionPlanFactory.create()
    fp_1 = FeaturePermission.objects.create(slug='ai_chatbot.access', name='AI Chatbot Access')
    fp_2 = FeaturePermission.objects.create(slug='analytics.view_dashboard', name='Dashboard Access')
    plan.feature_permissions.set([fp_1, fp_2])

    licenses = create_licenses_for_purchase(plan=plan, quantity=2)
    assert len(licenses) == 2

    for license_obj in licenses:
        assert set(license_obj.feature_permissions.values_list('slug', flat=True)) == {
            'ai_chatbot.access',
            'analytics.view_dashboard',
        }


@pytest.mark.django_db
def test_create_licenses_for_purchase_defaults_catalog_permission_when_plan_has_none():
    plan = SubscriptionPlanFactory.create()

    licenses = create_licenses_for_purchase(plan=plan, quantity=1)
    license_obj = licenses[0]
    assert set(license_obj.feature_permissions.values_list('slug', flat=True)) == {DEFAULT_CATALOG_FEATURE_SLUG}


@pytest.mark.django_db
def test_assign_license_sets_consumption_date():
    license_obj = LicenseFactory.create(status=constants.UNASSIGNED, consumption_date=None, user_email=None)

    result = assign_license(license_obj, {'email': 'learner@example.com'})
    assigned_license = result['license']
    assigned_license.refresh_from_db()

    assert assigned_license.status == constants.ASSIGNED
    assert assigned_license.consumption_date is not None
    assert assigned_license.user_email == 'learner@example.com'


@pytest.mark.django_db
def test_assign_license_blocks_reassignment_to_different_learner():
    now = localized_utcnow()
    license_obj = LicenseFactory.create(
        status=constants.ASSIGNED,
        consumption_date=now,
        assigned_date=now,
        user_email='first@example.com',
    )

    with pytest.raises(ValidationError):
        assign_license(license_obj, {'email': 'second@example.com'})


@pytest.mark.django_db
def test_expire_time_limited_licenses_ignores_perpetual():
    now = localized_utcnow()
    perpetual = LicenseFactory.create(
        status=constants.UNASSIGNED,
        expires_at=None,
    )
    time_limited_expired = LicenseFactory.create(
        status=constants.ASSIGNED,
        expires_at=now - timedelta(days=1),
    )
    active_time_limited = LicenseFactory.create(
        status=constants.ASSIGNED,
        expires_at=now + timedelta(days=1),
    )

    expired_count = expire_time_limited_licenses()
    assert expired_count == 1

    perpetual.refresh_from_db()
    time_limited_expired.refresh_from_db()
    active_time_limited.refresh_from_db()

    assert perpetual.status == constants.UNASSIGNED
    assert time_limited_expired.status == constants.REVOKED
    assert active_time_limited.status == constants.ASSIGNED


@pytest.mark.django_db
def test_license_perpetual_and_expired_properties():
    now = localized_utcnow()
    perpetual = LicenseFactory.create(expires_at=None)
    expired = LicenseFactory.create(expires_at=now - timedelta(days=1))
    active = LicenseFactory.create(expires_at=now + timedelta(days=1))

    assert perpetual.is_perpetual is True
    assert perpetual.is_expired is False
    assert expired.is_perpetual is False
    assert expired.is_expired is True
    assert active.is_expired is False
