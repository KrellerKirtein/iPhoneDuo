"""识别官网明确返回的预购闭店页；不把未知页面推断为闭店。"""
import re

import requests


class StoreClosedError(requests.HTTPError):
    """官网仍处于预购准备页面，保留真实响应状态及 Retry-After。"""


def is_closed_page(html):
    """只匹配已观测的完整闭店标记，普通商品页的预购文案不算闭店。"""
    return isinstance(html, str) and bool(re.search(r'"coversModeType"\s*:\s*"preorder_full"', html))


def check_closed_page(response):
    """在正常页面解析前识别闭店，避免 HTTP 200 封面被误报为模型或登录错误。"""
    if response.status_code in (200, 503) and is_closed_page(response.text):
        raise StoreClosedError('Apple 商城尚未开放，正在准备预购，请稍后重试。', response=response)
