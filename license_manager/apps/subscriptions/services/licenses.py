"""License lifecycle service helpers."""
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from license_manager.apps.subscriptions import constants
from license_manager.apps.subscriptions.models import (
    FeaturePermission,
    License,
)


DEFAULT_CATALOG_FEATURE_SLUG = 'catalog.curated_access'
DEFAULT_CATALOG_FEATURE_NAME = 'Catalog Curated Access'

_UNSET = object()


def ensure_default_catalog_feature_permission():
    """
    Ensure the default legacy catalog permission exists.
    """
    permission, _ = FeaturePermission.objects.get_or_create(
        slug=DEFAULT_CATALOG_FEATURE_SLUG,
        defaults={'name': DEFAULT_CATALOG_FEATURE_NAME},
    )
    return permission


def create_licenses_for_purchase(plan, quantity, expires_at=_UNSET):
    """
    Create licenses for a purchase and snapshot plan feature entitlements.

    Backward compatibility:
    - If ``expires_at`` is omitted, uses ``plan.expiration_date``.
    - If the plan has no feature permissions, attaches the default legacy catalog entitlement.
    """
    if quantity <= 0:
        return []

    if expires_at is _UNSET:
        resolved_expires_at = plan.expiration_date
    else:
        resolved_expires_at = expires_at

    plan_permissions = list(plan.feature_permissions.all())
    plan_roles = list(plan.feature_roles.all())
    if not plan_permissions and not plan_roles:
        plan_permissions = [ensure_default_catalog_feature_permission()]

    licenses = [
        License(
            subscription_plan=plan,
            expires_at=resolved_expires_at,
        ) for _ in range(quantity)
    ]
    License.bulk_create(licenses)

    through_model = License.feature_permissions.through
    through_model.objects.bulk_create([
        through_model(license_id=license.uuid, featurepermission_id=permission.id)
        for license in licenses
        for permission in plan_permissions
    ])
    role_through_model = License.feature_roles.through
    role_through_model.objects.bulk_create([
        role_through_model(license_id=license.uuid, featurerole_id=role.id)
        for license in licenses
        for role in plan_roles
    ])
    return licenses


def find_available_license(enterprise_id, feature_slug=None, role_slug=None, plan_id=None, user_id=None):  # pylint: disable=unused-argument
    """
    Find one available (unconsumed, unassigned, active) license matching requested constraints.
    """
    now = timezone.now()
    queryset = License.objects.filter(
        subscription_plan__customer_agreement__enterprise_customer_uuid=enterprise_id,
        consumption_date__isnull=True,
        status=constants.UNASSIGNED,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gte=now),
    )
    if feature_slug:
        queryset = queryset.filter(feature_permissions__slug=feature_slug)
    if role_slug:
        queryset = queryset.filter(feature_roles__slug=role_slug)
    if plan_id:
        queryset = queryset.filter(subscription_plan_id=plan_id)

    return queryset.distinct().order_by('created', 'uuid').first()


def _normalize_user(user):
    """
    Normalize a passed user-like object into email + lms_user_id.
    """
    if hasattr(user, 'email') or hasattr(user, 'id'):
        return getattr(user, 'email', None), getattr(user, 'id', None)
    if isinstance(user, dict):
        return user.get('email'), user.get('lms_user_id') or user.get('id')
    if isinstance(user, str):
        return user, None
    return None, None


def _is_same_learner(license_obj, target_email, target_lms_user_id):
    if license_obj.user_email and target_email:
        if license_obj.user_email.lower() != target_email.lower():
            return False
    if license_obj.lms_user_id and target_lms_user_id:
        if license_obj.lms_user_id != target_lms_user_id:
            return False
    return bool(
        (license_obj.user_email and target_email)
        or (license_obj.lms_user_id and target_lms_user_id)
    )


def assign_license(license_obj, user, assigned_by=None, metadata=None):  # pylint: disable=unused-argument
    """
    Assign a license to a learner, enforcing non-transferability.
    """
    target_email, target_lms_user_id = _normalize_user(user)
    if not target_email and not target_lms_user_id:
        raise ValidationError('A target learner identifier is required to assign a license.')

    with transaction.atomic():
        locked_license = License.objects.select_for_update().get(pk=license_obj.pk)

        if locked_license.is_expired:
            raise ValidationError('Cannot assign an expired license.')

        if locked_license.consumption_date is not None:
            if _is_same_learner(locked_license, target_email, target_lms_user_id):
                permission_slugs = sorted(
                    locked_license.feature_permissions.values_list('slug', flat=True)
                )
                role_slugs = sorted(
                    locked_license.feature_roles.values_list('slug', flat=True)
                )
                return {
                    'license': locked_license,
                    'granted_feature_slugs': permission_slugs,
                    'granted_permissions': permission_slugs,
                    'granted_roles': role_slugs,
                }
            raise ValidationError('This license has already been consumed and is non-transferable.')

        if locked_license.status in (constants.ASSIGNED, constants.ACTIVATED):
            raise ValidationError('This license has already been assigned.')

        now = timezone.now()
        if target_email:
            locked_license.user_email = target_email
        if target_lms_user_id:
            locked_license.lms_user_id = target_lms_user_id

        locked_license.status = constants.ASSIGNED
        locked_license.assigned_date = locked_license.assigned_date or now
        locked_license.last_remind_date = locked_license.last_remind_date or now
        locked_license.activation_key = locked_license.activation_key or uuid4()
        locked_license.consumption_date = locked_license.consumption_date or now
        locked_license.save()

        permission_slugs = sorted(
            locked_license.feature_permissions.values_list('slug', flat=True)
        )
        role_slugs = sorted(
            locked_license.feature_roles.values_list('slug', flat=True)
        )
        return {
            'license': locked_license,
            'granted_feature_slugs': permission_slugs,
            'granted_permissions': permission_slugs,
            'granted_roles': role_slugs,
        }


def expire_time_limited_licenses():
    """
    Expire licenses with explicit ``expires_at`` in the past.
    """
    now = timezone.now()
    licenses_to_expire = License.objects.filter(
        expires_at__isnull=False,
        expires_at__lt=now,
    ).exclude(status=constants.REVOKED)

    expired_count = 0
    for license_obj in licenses_to_expire.iterator():
        license_obj.status = constants.REVOKED
        if license_obj.revoked_date is None:
            license_obj.revoked_date = now
        license_obj.save(update_fields=['status', 'revoked_date'])
        expired_count += 1

    return expired_count
