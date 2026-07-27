"""The swappable user model.

Email is the identifier and there is no username column. This lands in the first
migration on purpose: swapping ``AUTH_USER_MODEL`` after any migration exists is a
well-known trap, and T-016 configures allauth for email-only login, which the stock
``auth.User`` can only emulate by generating throwaway usernames.
"""

import uuid
from typing import ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager["User"]):
    """Create users keyed on a normalized email address."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: object) -> "User":
        if not email:
            msg = "Users must have an email address."
            raise ValueError(msg)
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self,
        email: str,
        password: str | None = None,
        **extra: object,
    ) -> "User":
        """Create an ordinary, non-staff user."""
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(
        self,
        email: str,
        password: str | None = None,
        **extra: object,
    ) -> "User":
        """Create a superuser, refusing any caller that downgrades the flags."""
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra["is_staff"] is not True:
            msg = "Superuser must have is_staff=True."
            raise ValueError(msg)
        if extra["is_superuser"] is not True:
            msg = "Superuser must have is_superuser=True."
            raise ValueError(msg)
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """A person who signs in. Firm-side membership arrives in T-008."""

    # A UUID rather than a sequence: tenant-scoped tables carry foreign keys to this
    # global table, and PostgreSQL does not apply row-level security to the referenced
    # side of a foreign key, so the identifier must not be enumerable.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    full_name = models.CharField(_("full name"), max_length=255, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(_("staff status"), default=False)
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        """Model metadata."""

        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self) -> str:
        """Identify the user by the address they sign in with."""
        return self.email
