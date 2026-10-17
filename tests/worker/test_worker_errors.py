"""Worker 错误展示保留 HTTP 诊断信息，不泄露请求 URL 或令牌。"""
import unittest

import requests

from main import error_message


class WorkerErrorTests(unittest.TestCase):
    def test_closed_store_with_http_200_is_not_reported_as_network_or_account_failure(self):
        from store_status import StoreClosedError
        response = requests.Response()
        response.status_code = 200
        message = error_message(StoreClosedError('secret URL', response=response))
        self.assertIn('商城尚未开放', message)
        self.assertIn('未判定账号失效', message)
        self.assertNotIn('secret', message)
        self.assertNotIn('网络请求失败', message)

    def test_http_errors_show_status_without_request_details(self):
        for status in (429, 503, 403):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                response.url = 'https://secure.www.apple.com.cn/shop/account/home?token=secret'
                error = requests.HTTPError('sensitive response ' + response.url, response=response)
                message = error_message(error)
                self.assertIn(str(status), message)
                self.assertNotIn('token', message)
                self.assertNotIn('secret', message)
                self.assertNotIn('sensitive', message)
                if status in (429, 503):
                    self.assertIn('未判定账号失效', message)

    def test_transport_and_unknown_errors_never_expose_exception_details(self):
        self.assertEqual(error_message(requests.Timeout('secret URL')),
                         '网络请求失败，请检查网络或系统代理。')
        self.assertEqual(error_message(RuntimeError('secret URL')),
                         '操作未完成（RuntimeError），请查看任务阶段。')
        self.assertEqual(error_message(ValueError('safe business error')), 'safe business error')


if __name__ == '__main__':
    unittest.main()
