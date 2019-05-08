from __future__ import absolute_import

from django.contrib.auth import authenticate
from django.contrib.sites.models import Site
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import PermissionDenied
from django.db import models
from django.utils.crypto import get_random_string

import allauth.app_settings
from allauth.account.models import EmailAddress
from allauth.account.utils import get_next_redirect_url, setup_user_email
from allauth.compat import (
    force_str,
    python_2_unicode_compatible,
    ugettext_lazy as _,
)
from allauth.utils import get_user_model

from ..utils import get_request_param
from . import app_settings, providers
from .adapter import get_adapter
from .fields import JSONField


class SocialAppManager(models.Manager):
    def get_current(self, provider, request=None):
        cache = {}
        if request:
            cache = getattr(request, '_socialapp_cache', {})
            request._socialapp_cache = cache
        app = cache.get(provider)
        if not app:
            site = get_current_site(request)
            app = self.get(
                sites__id=site.id,
                provider=provider)
            cache[provider] = app
        return app


@python_2_unicode_compatible
class SocialApp(models.Model):
    objects = SocialAppManager()

    provider = models.CharField(verbose_name=_('provider'),
                                max_length=30,
                                choices=providers.registry.as_choices())
    name = models.CharField(verbose_name=_('name'),
                            max_length=40)
    type = models.CharField(verbose_name=_('Type'),
                            max_length=191,
                            blank=True,
                            help_text=_('Type, level or purpose of key'))
    client_email = models.EmailField(null=True,
                                     blank=True)
    client_id = models.CharField(verbose_name=_('client id'),
                                 max_length=191,
                                 help_text=_('App ID, or consumer key'))
    secret = models.CharField(verbose_name=_('secret key'),
                              max_length=191,
                              help_text=_('API secret, client secret, or'
                                          ' consumer secret'))
    key = models.CharField(verbose_name=_('key'),
                           max_length=191,
                           blank=True,
                           help_text=_('Key'))
    pem = models.TextField(verbose_name=_('PEM'),
                           blank=True,
                           help_text=_('Super long private key, usually'
                                       ' begins with: -----BEGIN PRIVATE KEY-----.'
                                       ' This is an unsecure way to store very'
                                       ' important keys'))
    # Most apps can be used across multiple domains, therefore we use
    # a ManyToManyField. Note that Facebook requires an app per domain
    # (unless the domains share a common base name).
    # blank=True allows for disabling apps without removing them
    sites = models.ManyToManyField(Site, blank=True)

    class Meta:
        verbose_name = _('social application')
        verbose_name_plural = _('social applications')

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class SocialAccount(models.Model):
    user = models.ForeignKey(allauth.app_settings.USER_MODEL,
                             on_delete=models.CASCADE)
    provider = models.CharField(verbose_name=_('provider'),
                                max_length=30,
                                choices=providers.registry.as_choices())
    # Just in case you're wondering if an OpenID identity URL is going
    # to fit in a 'uid':
    #
    # Ideally, URLField(max_length=1024, unique=True) would be used
    # for identity.  However, MySQL has a max_length limitation of 191
    # for URLField (in case of utf8mb4). How about
    # models.TextField(unique=True) then?  Well, that won't work
    # either for MySQL due to another bug[1]. So the only way out
    # would be to drop the unique constraint, or switch to shorter
    # identity URLs. Opted for the latter, as [2] suggests that
    # identity URLs are supposed to be short anyway, at least for the
    # old spec.
    #
    # [1] http://code.djangoproject.com/ticket/2495.
    # [2] http://openid.net/specs/openid-authentication-1_1.html#limits

    uid = models.CharField(verbose_name=_('uid'),
                           max_length=app_settings.UID_MAX_LENGTH)
    last_login = models.DateTimeField(verbose_name=_('last login'),
                                      auto_now=True)
    date_joined = models.DateTimeField(verbose_name=_('date joined'),
                                       auto_now_add=True)
    extra_data = JSONField(verbose_name=_('extra data'), default=dict)

    class Meta:
        unique_together = ('provider', 'uid')
        verbose_name = _('social account')
        verbose_name_plural = _('social accounts')

    def authenticate(self):
        return authenticate(account=self)

    def __str__(self):
        return force_str(self.user)

    def get_profile_url(self):
        return self.get_provider_account().get_profile_url()

    def get_avatar_url(self):
        return self.get_provider_account().get_avatar_url()

    def get_provider(self):
        return providers.registry.by_id(self.provider)

    def get_provider_account(self):
        return self.get_provider().wrap_account(self)


@python_2_unicode_compatible
class SocialToken(models.Model):
    app = models.ForeignKey(SocialApp, on_delete=models.CASCADE)
    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE)
    active = models.BooleanField(default=True)
    token = models.TextField(
        verbose_name=_('token'),
        help_text=_(
            '"oauth_token" (OAuth1) or access token (OAuth2)'))
    token_secret = models.TextField(
        blank=True,
        verbose_name=_('token secret'),
        help_text=_(
            '"oauth_token_secret" (OAuth1) or refresh token (OAuth2)'))
    expires_at = models.DateTimeField(blank=True, null=True,
                                      verbose_name=_('expires at'))

    class Meta:
        unique_together = ('app', 'account')
        verbose_name = _('social application token')
        verbose_name_plural = _('social application tokens')

    def __str__(self):
        return self.token


class SocialLogin(object):
    """
    Represents a social user that is in the process of being logged
    in. This consists of the following information:

    `account` (`SocialAccount` instance): The social account being
    logged in. Providers are not responsible for checking whether or
    not an account already exists or not. Therefore, a provider
    typically creates a new (unsaved) `SocialAccount` instance. The
    `User` instance pointed to by the account (`account.user`) may be
    prefilled by the provider for use as a starting point later on
    during the signup process.

    `token` (`SocialToken` instance): An optional access token token
    that results from performing a successful authentication
    handshake.

    `state` (`dict`): The state to be preserved during the
    authentication handshake. Note that this state may end up in the
    url -- do not put any secrets in here. It currently only contains
    the url to redirect to after login.

    `email_addresses` (list of `EmailAddress`): Optional list of
    e-mail addresses retrieved from the provider.
    """

    def __init__(self, user=None, account=None, token=None,
                 email_addresses=[]):
        if token:
            assert token.account is None or token.account == account
        self.token = token
        self.user = user
        self.account = account
        self.email_addresses = email_addresses
        self.state = {}

    def connect(self, request, user):
        self.user = user
        self.save(request, connect=True)

    def serialize(self):
        serialize_instance = get_adapter().serialize_instance
        ret = dict(account=serialize_instance(self.account),
                   user=serialize_instance(self.user),
                   state=self.state,
                   email_addresses=[serialize_instance(ea)
                                    for ea in self.email_addresses])
        if self.token:
            ret['token'] = serialize_instance(self.token)
        return ret

    @classmethod
    def deserialize(cls, data):
        deserialize_instance = get_adapter().deserialize_instance
        account = deserialize_instance(SocialAccount, data['account'])
        user = deserialize_instance(get_user_model(), data['user'])
        if 'token' in data:
            token = deserialize_instance(SocialToken, data['token'])
        else:
            token = None
        email_addresses = []
        for ea in data['email_addresses']:
            email_address = deserialize_instance(EmailAddress, ea)
            email_addresses.append(email_address)
        ret = cls()
        ret.token = token
        ret.account = account
        ret.user = user
        ret.email_addresses = email_addresses
        ret.state = data['state']
        return ret

    def save(self, request, connect=False):
        """
        Saves a new account. Note that while the account is new,
        the user may be an existing one (when connecting accounts)
        """
        assert not self.is_existing
        user = self.user
        user.save()
        self.account.user = user
        self.account.save()
        if app_settings.STORE_TOKENS and self.token:
            self.token.account = self.account
            self.token.save()
        if connect:
            # TODO: Add any new email addresses automatically?
            pass
        else:
            setup_user_email(request, user, self.email_addresses)

    @property
    def is_existing(self):
        """
        Account is temporary, not yet backed by a database record.
        """
        return self.account.pk

    def lookup(self):
        """
        Lookup existing account, if any.
        """
        assert not self.is_existing
        try:
            a = SocialAccount.objects.get(provider=self.account.provider,
                                          uid=self.account.uid)
            # Update account
            a.extra_data = self.account.extra_data
            self.account = a
            self.user = self.account.user
            a.save()
            # Update token
            if app_settings.STORE_TOKENS and self.token:
                assert not self.token.pk
                try:
                    t = SocialToken.objects.get(account=self.account,
                                                app=self.token.app)
                    t.token = self.token.token
                    if self.token.token_secret:
                        # only update the refresh token if we got one
                        # many oauth2 providers do not resend the refresh token
                        t.token_secret = self.token.token_secret
                    t.expires_at = self.token.expires_at
                    t.save()
                    self.token = t
                except SocialToken.DoesNotExist:
                    self.token.account = a
                    self.token.save()
        except SocialAccount.DoesNotExist:
            pass

    def get_redirect_url(self, request):
        url = self.state.get('next')
        return url

    @classmethod
    def state_from_request(cls, request):
        state = {}
        next_url = get_next_redirect_url(request)
        if next_url:
            state['next'] = next_url
        state['process'] = get_request_param(request, 'process', 'login')
        state['scope'] = get_request_param(request, 'scope', '')
        state['auth_params'] = get_request_param(request, 'auth_params', '')
        return state

    @classmethod
    def stash_state(cls, request):
        state = cls.state_from_request(request)
        verifier = get_random_string()
        request.session['socialaccount_state'] = (state, verifier)
        return verifier

    @classmethod
    def unstash_state(cls, request):
        if 'socialaccount_state' not in request.session:
            raise PermissionDenied()
        state, verifier = request.session.pop('socialaccount_state')
        return state

    @classmethod
    def verify_and_unstash_state(cls, request, verifier):
        if 'socialaccount_state' not in request.session:
            raise PermissionDenied()
        state, verifier2 = request.session.pop('socialaccount_state')
        if verifier != verifier2:
            raise PermissionDenied()
        return state
