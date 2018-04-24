# Author: Krzysztof Dorosz <cypreess@gmail.com>
#
# Disclaimer:
# Writing and open sourcing this backend was kindly funded by Issue Stand
# http://issuestand.com/
#

from decimal import Decimal
import hashlib
import logging
import time
import datetime
from django.utils import six
from six.moves.urllib.request import Request, urlopen
from six.moves.urllib.parse import urlencode, parse_qs

from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from django.utils.translation import ugettext_lazy as _
from django.apps import apps
from pytz import utc

from getpaid import signals
from getpaid.backends import PaymentProcessorBase
from getpaid.utils import get_domain

logger = logging.getLogger('getpaid.backends.przelewy24')


class PaymentProcessor(PaymentProcessorBase):
    BACKEND = u'getpaid.backends.przelewy24'
    BACKEND_NAME = _(u'Przelewy24')
    BACKEND_ACCEPTED_CURRENCY = (u'PLN', u'EUR', u'GBP', u'CZK')
    BACKEND_LOGO_URL = u'getpaid/backends/przelewy24/przelewy24_logo.png'

    _REGISTER_URL = u'https://secure.przelewy24.pl/trnRegister'
    _SANDBOX_REGISTER_URL = u'https://sandbox.przelewy24.pl/trnRegister'

    _GATEWAY_URL = u'https://secure.przelewy24.pl/trnRequest/'
    _SANDBOX_GATEWAY_URL = u'https://sandbox.przelewy24.pl/trnRequest/'

    _GATEWAY_CONFIRM_URL = u'https://secure.przelewy24.pl/trnVerify'
    _SANDBOX_GATEWAY_CONFIRM_URL = u'https://sandbox.przelewy24.pl/trnVerify'

    _ACCEPTED_LANGS = (u'pl', u'en', u'es', u'de', u'it')
    _REQUEST_SIG_FIELDS = (u'p24_session_id', u'p24_merchant_id', u'p24_amount', u'p24_currency', u'crc')
    _SUCCESS_RETURN_SIG_FIELDS = (u'p24_session_id', u'p24_order_id', u'p24_amount', u'p24_currency', u'crc')
    _STATUS_SIG_FIELDS = (u'p24_session_id', u'p24_order_id', u'p24_amount', u'p24_currency', u'crc')

    @staticmethod
    def compute_sig(params, fields, crc):
        params = params.copy()
        params.update({'crc': crc})
        text = u"|".join(map(lambda field: six.text_type(params.get(field, '')), fields))
        return six.text_type(hashlib.md5(text.encode('utf-8')).hexdigest())

    @staticmethod
    def on_payment_status_change(p24_session_id, p24_order_id, p24_amount, p24_currency, p24_sign):
        params = {
            'p24_session_id': p24_session_id,
            'p24_order_id': p24_order_id,
            'p24_amount': p24_amount,
            'p24_currency': p24_currency,
            'p24_sign': p24_sign,
        }
        crc = PaymentProcessor.get_backend_setting('crc')
        if p24_sign != PaymentProcessor.compute_sig(params, PaymentProcessor._SUCCESS_RETURN_SIG_FIELDS, crc):
            logger.warning('Success return call has wrong crc %s' % str(params))
            return False

        Payment = apps.get_model('getpaid', 'Payment')
        payment_id = p24_session_id.split(':')[0]
        try:
            payment = Payment.objects.get(pk=int(payment_id))
            processor = PaymentProcessor(payment)
            processor.get_payment_status(p24_session_id, p24_order_id, p24_amount)
        except:
            return False
        return True

    def get_payment_status(self, p24_session_id, p24_order_id, p24_amount):
        merchant_id = PaymentProcessor.get_backend_setting('id')
        params = {
            'p24_merchant_id': merchant_id,
            'p24_pos_id': PaymentProcessor.get_backend_setting('pos_id', merchant_id),
            'p24_session_id': p24_session_id,
            'p24_amount': int(self.payment.amount * 100),
            'p24_currency': self.payment.currency,
            'p24_order_id': p24_order_id,
        }
        crc = PaymentProcessor.get_backend_setting('crc')
        params['p24_sign'] = PaymentProcessor.compute_sig(params, self._STATUS_SIG_FIELDS, crc)

        for key in params.keys():
            params[key] = six.text_type(params[key]).encode('utf-8')

        data = urlencode(params).encode('utf-8')

        url = self._GATEWAY_CONFIRM_URL
        if PaymentProcessor.get_backend_setting('sandbox', False):
            url = self._SANDBOX_GATEWAY_CONFIRM_URL

        self.payment.external_id = p24_order_id

        request = Request(url, data)
        try:
            response = urlopen(request).read().decode('utf8')
        except Exception:
            logger.exception('Error while getting payment status change %s data=%s' % (url, str(params)))
            return

        response_data = parse_qs(response)
        if response_data['error'][0] == '0':
            logger.info('Payment accepted %s' % str(params))
            self.payment.amount_paid = Decimal(p24_amount) / Decimal('100')
            self.payment.paid_on = datetime.datetime.utcnow().replace(tzinfo=utc)
            if self.payment.amount_paid >= self.payment.amount:
                self.payment.change_status('paid')
            else:
                self.payment.change_status('partially_paid')
        else:
            logger.warning('Payment rejected for data=%s: "%s"' % (str(params), response))
            self.payment.change_status('failed')

    def get_gateway_url(self, request):
        """
        Routes a payment to Gateway, should return URL for redirection.

        """
        merchant_id = PaymentProcessor.get_backend_setting('id')
        params = {
            'p24_merchant_id': merchant_id,
            'p24_pos_id': PaymentProcessor.get_backend_setting('pos_id', merchant_id),
            'p24_description': self.get_order_description(
                self.payment, self.payment.order),
            'p24_session_id': "%s:%s:%s" % (
                self.payment.pk, self.BACKEND, time.time()),
            'p24_amount': int(self.payment.amount * 100),
            'p24_email': None,
            'p24_currency': self.payment.currency,
            'p24_encoding': 'UTF-8',

        }

        user_data = {
            'email': None,
            'lang': None,
            'p24_client': None,
            'p24_address': None,
            'p24_zip': None,
            'p24_city': None,
            'p24_country': 'PL',
            'p24_phone': None,
        }
        signals.user_data_query.send(
            sender=None, order=self.payment.order, user_data=user_data)

        for key in ('p24_client', 'p24_address', 'p24_zip', 'p24_city',
                    'p24_country', 'p24_phone'):
            if user_data[key] is not None:
                params[key] = user_data[key]

        if user_data['email']:
            params['p24_email'] = user_data['email']

        if user_data['lang'] and user_data['lang'].lower() in PaymentProcessor._ACCEPTED_LANGS:
            params['p24_language'] = user_data['lang'].lower()
        elif PaymentProcessor.get_backend_setting('lang', False) and PaymentProcessor.get_backend_setting(
                'lang').lower() in PaymentProcessor._ACCEPTED_LANGS:
            params['p24_language'] = PaymentProcessor.get_backend_setting('lang').lower()

        params['p24_sign'] = self.compute_sig(params, self._REQUEST_SIG_FIELDS,
                                              PaymentProcessor.get_backend_setting('crc'))
        params['p24_api_version'] = '3.2'

        current_site = get_domain()
        use_ssl = PaymentProcessor.get_backend_setting('ssl_return', False)

        params['p24_url_return'] = ('https://' if use_ssl else 'http://') + current_site + reverse(
            'getpaid:przelewy24:return', kwargs={'pk': self.payment.pk})
        params['p24_url_status'] = ('https://' if use_ssl else 'http://') + current_site + reverse(
            'getpaid:przelewy24:status')

        if params['p24_email'] is None:
            raise ImproperlyConfigured(
                '%s requires filling `email` field for payment '
                '(you need to handle `user_data_query` signal)' % self.BACKEND)

        url = self._SANDBOX_REGISTER_URL if PaymentProcessor.get_backend_setting(
            'sandbox', False) else self._REGISTER_URL
        data = urlencode(params).encode('utf-8')
        request = Request(url, data)
        response = urlopen(request).read().decode('utf8')
        response_data = parse_qs(response)
        if response_data['error'][0] == '0':
            token = response_data['token'][0]
        else:
            # TODO
            raise Exception(response_data['error'])

        request_url = self._SANDBOX_GATEWAY_URL if PaymentProcessor.get_backend_setting(
            'sandbox', False) else self._GATEWAY_URL
        return request_url + token, 'GET', {}
