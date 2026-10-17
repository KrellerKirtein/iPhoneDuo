"""明确闭店页通过真实 HTTP 响应进入公共请求边界，未知页面仍交给原流程。"""
import unittest
from unittest.mock import patch

import requests
from urllib3.response import HTTPResponse

import duo_killer as shop
from store_status import StoreClosedError, check_closed_page


def response(status, html):
    reply = requests.Response()
    reply.status_code = status
    reply._content = html.encode('utf-8')
    reply.raw = HTTPResponse(body=reply._content, headers={})
    reply.url = shop.BASE_URL + '/shop/bag'
    reply.request = requests.Request('GET', reply.url).prepare()
    reply.headers['Retry-After'] = '90'
    return reply


class ClosedStoreTests(unittest.TestCase):
    def test_login_503_preserves_retry_after_through_business_wrapper(self):
        from apple_login import AppleStoreLogin, LoginError
        import preorder
        reply = response(503, 'Service unavailable')
        with self.assertRaises(LoginError) as caught:
            AppleStoreLogin.expect(reply, (200,))
        error = ValueError('login temporarily unavailable')
        error.__cause__ = caught.exception
        self.assertTrue(preorder.is_store_unavailable(error))
        self.assertEqual(preorder.retry_after(error), '90')

    def test_timed_add_preserves_http_200_closed_cover_for_scheduler(self):
        with requests.Session() as session:
            reply = response(200, '{"coversModeType":"preorder_full"}')
            error = StoreClosedError(response=reply)
            prepared = {'product': 'MLH74FE/A', 'apikey': 'key', 'atbtoken': 'token'}
            with patch.object(shop, 'prepare_add_to_bag', return_value=prepared), \
                    patch.object(shop, 'request_get', side_effect=error), \
                    self.assertRaises(StoreClosedError) as caught:
                shop._add_one_to_bag(session, 'MLH74FE/A', preserve_retry_after=True)
            self.assertIs(caught.exception, error)

    def test_shared_request_rejects_explicit_closed_cover_and_preserves_response(self):
        for status in (200, 503):
            with self.subTest(status=status), requests.Session() as session:
                reply = response(status, '<script>{"coversModeType" : "preorder_full"}</script>')
                with patch.object(session, 'request', return_value=reply), \
                        patch('duo_killer.interaction.report_stage'):
                    with self.assertRaises(StoreClosedError) as caught:
                        shop.request_get(session, '测试读取购物袋', reply.url)
                self.assertIs(caught.exception.response, reply)
                self.assertEqual(caught.exception.response.status_code, status)
                self.assertEqual(caught.exception.response.headers['Retry-After'], '90')

    def test_unknown_pages_and_auth_errors_are_not_classified_as_closed(self):
        for status, html in ((200, 'Preorder coming soon'),
                             (200, '{"coversModeType":"unknown"}'),
                             (503, 'Service unavailable'),
                             (401, '{"coversModeType":"preorder_full"}')):
            with self.subTest(status=status, html=html):
                check_closed_page(response(status, html))


if __name__ == '__main__':
    unittest.main()
