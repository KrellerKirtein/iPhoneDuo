import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import duo_killer


class SharedCartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.auth = self.root / 'auth.json'
        self.auth.write_text(json.dumps({'account': 'test@example.invalid', 'password': 'secret', 'cookies': {
            'account_session': [], 'server_managed': [], 'with_expiry': [], 'expiry_unknown': []}}))
        for target, name, value in ((duo_killer, 'ROOT', self.root), (duo_killer, 'AUTH_PATH', self.auth)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (('duo_killer.ensure_strong_login', {'return_value': False}),
                               ('requests.Session.request', {'side_effect': AssertionError('禁止真实网络和下单')})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    @staticmethod
    def cart(quantity, token, checkout_action=True):
        return {'meta': {'h': {'x-aos-stk': token}}, 'shoppingCart': {
            'actions': {'a': {'checkout': {'url': '/shop/bagx/checkout_now'}}} if checkout_action else {},
            'summary': {'d': {'total': f'RMB {quantity * 69}'}},
            'items': {'item-cloth': {'d': {'sku': 'MLH74FE/A', 'totalPrice': f'RMB {quantity * 69}'},
                'itemQuantity': {'d': {'quantity': quantity}, 'a': {'updateQuantity': {'url': '/shop/bagx'}}}}}}}

    @staticmethod
    def response(data, url, html=False):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        body = json.dumps(data)
        if html:
            body = '<meta name="globalnav-store-key" content="current-key"><script id="init_data">' + body + '</script>'
        response._content = body.encode()
        return response

    def test_same_session_and_updated_cart_token_reach_checkout_without_extra_bag_get(self):
        def request(session, stage, url, **kwargs):
            if stage == '加购前购物袋':
                return self.response(self.cart(1, 'before'), url, html=True)
            if stage == '检查商城登录':
                self.assertEqual(kwargs['params']['apikey'], 'current-key')
                return self.response({'links': [{'type': 'signOut'}]}, url)
            if stage == 'updateQuantity':
                self.assertEqual(kwargs['headers']['x-aos-stk'], 'before')
                return self.response({'head': {'status': 200}, 'body': self.cart(2, 'after')}, url)
            if stage == 'checkout':
                self.assertEqual(kwargs['headers']['x-aos-stk'], 'after')
                self.assertEqual(kwargs['data']['shoppingCart.items.item-cloth.itemQuantity.quantity'], '2')
                raise ValueError('已验证结账请求，不实际下单')
            self.fail('不应出现额外请求：' + stage)
        with patch.object(duo_killer, 'request_get', side_effect=request) as calls, \
                patch.object(duo_killer, 'save_auth') as save, \
                self.assertRaisesRegex(ValueError, '已验证结账请求'):
            duo_killer.purchase_sku('MLH74FE', quantity=2)
        self.assertEqual([call.args[1] for call in calls.call_args_list],
                         ['加购前购物袋', '检查商城登录', 'updateQuantity', 'checkout'])
        self.assertEqual(len({id(call.args[0]) for call in calls.call_args_list}), 1)
        save.assert_called_once()

    def test_relogin_discards_old_cart_and_uses_fresh_cookie_and_token(self):
        def login(**kwargs):
            data = json.loads(self.auth.read_text())
            data['cookies']['account_session'] = [{'name': 'as_ltn_cn', 'value': 'fresh-session',
                                                  'domain': '.apple.com.cn', 'path': '/'}]
            self.auth.write_text(json.dumps(data))
        def request(session, stage, url, **kwargs):
            if stage == '加购前购物袋':
                return self.response(self.cart(1, 'old'), url, html=True)
            if stage == '检查商城登录':
                return self.response({'links': [{'type': 'signIn'}]}, url)
            if stage == '重新登录后购物袋':
                self.assertEqual(session.cookies.get('as_ltn_cn'), 'fresh-session')
                return self.response(self.cart(2, 'fresh'), url, html=True)
            if stage == 'checkout':
                self.assertEqual(kwargs['headers']['x-aos-stk'], 'fresh')
                raise ValueError('已验证新会话')
            self.fail('不应出现额外请求：' + stage)
        with patch.object(duo_killer, 'request_get', side_effect=request) as calls, \
                patch.object(duo_killer, 'login', side_effect=login) as auth, \
                self.assertRaisesRegex(ValueError, '已验证新会话'):
            duo_killer.purchase_sku('MLH74FE', quantity=2)
        auth.assert_called_once_with(sms=False)
        self.assertEqual([c.args[1] for c in calls.call_args_list],
                         ['加购前购物袋', '检查商城登录', '重新登录后购物袋', 'checkout'])

    def test_missing_checkout_action_falls_back_to_fresh_bag(self):
        with requests.Session() as session:
            flow = duo_killer.Checkout(session)
            flow.update(self.cart(2, 'partial', checkout_action=False))
            def request(current, stage, url, **kwargs):
                if stage == '结账购物袋':
                    return self.response(self.cart(2, 'refreshed'), url, html=True)
                self.assertEqual(stage, 'checkout')
                self.assertEqual(kwargs['headers']['x-aos-stk'], 'refreshed')
                raise ValueError('已验证刷新')
            with patch.object(duo_killer, 'request_get', side_effect=request) as calls, \
                    self.assertRaisesRegex(ValueError, '已验证刷新'):
                duo_killer.pay_bag(flow=flow)
            self.assertEqual([c.args[1] for c in calls.call_args_list], ['结账购物袋', 'checkout'])

    def test_failed_login_does_not_restore_old_session_cookies(self):
        data = json.loads(self.auth.read_text())
        data['cookies']['account_session'] = [{'name': 'as_ltn_cn', 'value': 'old-session',
                                              'domain': '.apple.com.cn', 'path': '/'}]
        self.auth.write_text(json.dumps(data))
        def failed_login(**kwargs):
            data = json.loads(self.auth.read_text())
            data['cookies']['account_session'] = []
            data['password'] = ''
            self.auth.write_text(json.dumps(data))
            raise ValueError('账号或密码错误')
        def request(session, stage, url, **kwargs):
            if stage == '加购前购物袋':
                return self.response(self.cart(1, 'old'), url, html=True)
            self.assertEqual(stage, '检查商城登录')
            return self.response({'links': [{'type': 'signIn'}]}, url)
        with patch.object(duo_killer, 'request_get', side_effect=request), \
                patch.object(duo_killer, 'login', side_effect=failed_login), \
                patch.object(duo_killer, 'add_to_bag') as add, patch.object(duo_killer, 'pay_bag') as pay, \
                self.assertRaisesRegex(ValueError, '账号或密码错误'):
            duo_killer.purchase_sku('MLH74FE', quantity=2)
        saved = json.loads(self.auth.read_text())
        self.assertEqual(saved['cookies']['account_session'], [])
        self.assertEqual(saved['password'], '')
        add.assert_not_called()
        pay.assert_not_called()

    def test_fresh_add_preparation_skips_product_and_beacon_requests(self):
        with requests.Session() as session:
            session.cookies.set('as_atb', 'a|b|current', domain='.apple.com.cn', path='/')
            prepared = {'product': 'MLH74FE/A', 'apikey': 'prepared-key', 'atbtoken': 'current', 'prepared_at': 100}
            response = self.response({'addedToBag': True}, duo_killer.BASE_URL + '/cn/shop/bag/add')
            with patch.object(duo_killer, 'perf_counter', return_value=110), \
                    patch.object(duo_killer, 'prepare_add_to_bag') as prepare, \
                    patch.object(duo_killer, 'request_get', return_value=response) as request:
                self.assertEqual(duo_killer._add_one_to_bag(session, 'MLH74FE/A', prepared=prepared), '')
            prepare.assert_not_called()
            request.assert_called_once()
            self.assertEqual(request.call_args.args[1], '执行加购')
            self.assertEqual(request.call_args.kwargs['params']['apikey'], 'prepared-key')

    def test_expired_or_changed_cookie_never_replays_prepared_token(self):
        with requests.Session() as session:
            session.cookies.set('as_atb', 'a|b|current', domain='.apple.com.cn', path='/')
            response = self.response({'addedToBag': True}, duo_killer.BASE_URL + '/cn/shop/bag/add')
            fresh = {'product': 'MLH74FE/A', 'apikey': 'new-key', 'atbtoken': 'new-token', 'prepared_at': 120}
            for age, token in ((16, 'current'), (1, 'old-token')):
                prepared = {'product': 'MLH74FE/A', 'apikey': 'old-key', 'atbtoken': token, 'prepared_at': 120 - age}
                with self.subTest(age=age, token=token), patch.object(duo_killer, 'perf_counter', return_value=120), \
                        patch.object(duo_killer, 'prepare_add_to_bag', return_value=fresh) as prepare, \
                        patch.object(duo_killer, 'request_get', return_value=response) as request:
                    duo_killer._add_one_to_bag(session, 'MLH74FE/A', prepared=prepared)
                prepare.assert_called_once_with(session, 'MLH74FE/A')
                self.assertEqual(request.call_args.kwargs['params']['atbtoken'], 'new-token')

    def test_add_rate_limit_keeps_retry_after_for_scheduler(self):
        with requests.Session() as session:
            prepared = {'product': 'MLH74FE/A', 'apikey': 'key', 'atbtoken': 'token', 'prepared_at': 100}
            response = self.response({}, duo_killer.BASE_URL + '/cn/shop/bag/add')
            response.status_code = 429
            response.headers['Retry-After'] = '60'
            with patch.object(duo_killer, 'prepare_add_to_bag', return_value=prepared), \
                    patch.object(duo_killer, 'request_get', return_value=response), \
                    self.assertRaises(duo_killer.TemporaryCheckoutError) as caught:
                duo_killer._add_one_to_bag(session, 'MLH74FE/A', preserve_retry_after=True)
            self.assertIs(caught.exception.response, response)

    def test_manual_add_keeps_read_bag_recovery_on_rate_limit(self):
        with requests.Session() as session:
            response = self.response({}, duo_killer.BASE_URL + '/cn/shop/bag/add')
            response.status_code = 429
            with patch.object(duo_killer, 'prepare_add_to_bag', return_value={
                    'product': 'MLH74FE/A', 'apikey': 'key', 'atbtoken': 'token'}), \
                    patch.object(duo_killer, 'request_get', return_value=response):
                self.assertEqual(duo_killer._add_one_to_bag(session, 'MLH74FE/A'), '加购接口返回 HTTP 429。')


if __name__ == '__main__':
    unittest.main()
