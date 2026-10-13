"""Desktop boundary: legacy network operations remain account-local and sequential."""
import base64
import json
import logging
import re
from pathlib import Path

import requests
import interaction
import duo_killer
import alipay_qr
import checkout_options
from credentials import Vault


def empty_auth(account):
    return {'account': account, 'cookies': {key: [] for key in
            ('account_session', 'server_managed', 'with_expiry', 'expiry_unknown')}, 'trust_cookies': []}


class Service:
    def __init__(self, channel, account_dir, task_dir, account, vault=None):
        self.channel, self.root, self.task_dir, self.account = channel, account_dir, task_dir, account
        self.vault = vault or Vault()
        self.password = None
        self.remember = False
        self.authentication_requested = False
        self.authentication_failed = False
        self.action = None
        self.submitted = False
        self.preorder_policy = None
        self.auth = self.root / 'auth.json'
        self.checkout = self.root / 'checkout.json'
        duo_killer.ROOT, duo_killer.AUTH_PATH, duo_killer.LOG_PATH = self.root, self.auth, self.task_dir / 'requests.log'
        duo_killer.PAYMENT_ROOT = self.task_dir
        alipay_qr.ROOT = self.task_dir
        interaction.handler = self

    def enable_logging(self):
        from protocol import LogStream
        from platform_support import private_fd
        import os
        with os.fdopen(os.open(duo_killer.LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as file:
            private_fd(file.fileno())
        logger = duo_killer.log
        logger.disabled = False
        logger.propagate = False
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s %(message)s', '%H:%M:%S')
        for handler in (logging.FileHandler(duo_killer.LOG_PATH, encoding='utf-8'), logging.StreamHandler(LogStream(self.channel))):
            handler.setFormatter(formatter)
            logger.addHandler(handler)

    def ensure_auth_file(self):
        if self.auth.exists():
            data = json.loads(self.auth.read_text(encoding='utf-8'))
            if data.get('account', '').casefold() != self.account.casefold():
                raise ValueError('本地会话的账号与所选账号不同，请重新导入正确的会话。')
            if 'password' in data:
                data.pop('password')
                duo_killer.write_private_json(self.auth, data)
            return False
        duo_killer.write_private_json(self.auth, empty_auth(self.account))
        return True

    def credentials(self, path):
        self.authentication_requested = True
        if not self.password:
            try:
                self.password = self.vault.get(self.channel.account_id)
            except Exception:
                self.stage('系统凭据保险库不可用，请输入本次登录密码')
        if not self.password:
            value = self.channel.prompt('password', '输入 Apple 账号密码', account=self.account)
            if not isinstance(value, dict) or not isinstance(value.get('password'), str) or not value['password']:
                raise ValueError('请输入密码。')
            self.password = value['password']
            self.remember = value.get('remember') is True
        return self.account, self.password

    def text(self, label, default=''):
        if self.preorder_policy and '验证码' not in label:
            raise ValueError('自动计划缺少必要信息：' + label)
        kind = 'code' if '验证码' in label else 'text'
        label = label.replace('👉 ', '')
        fields = {}
        if kind == 'code':
            # Expose only the transitions supported by this legacy prompt.
            for hint, value, title in (
                ('（输入 s 改用短信）', 's', '改用短信验证码'),
                ('（输入 d 改用设备）', 'd', '改用设备验证码'),
            ):
                if hint in label:
                    label = label.replace(hint, '')
                    fields['actions'] = [{'value': value, 'label': title}]
        value = self.channel.prompt(kind, label, default=default, **fields)
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError('输入格式不正确。')
        return value

    def choose(self, label, options, default=None):
        if self.preorder_policy:
            raise ValueError('自动计划遇到尚未预设的选项：' + label)
        return self.channel.prompt('select', label, options=[{'value': k, 'label': v} for k, v in options], default=default)

    def confirm(self, details):
        if self.preorder_policy:
            return self.preorder_policy.confirm(details)
        return self.channel.prompt('confirm', '确认商品与金额，创建订单', details=details, account=self.account)

    def stage(self, stage, **fields):
        self.authentication_failed = self.authentication_failed or fields.get('authentication_failed', False)
        if 'browser_pid' in fields:
            self.channel.emit('stage', message=stage, **fields)
            self.channel.checkpoint()
            return
        self.channel.checkpoint()
        self.submitted = self.submitted or fields.get('order_submitted', False)
        self.channel.emit('stage', message=stage, **fields)

    def persist_password(self):
        if self.remember and self.password:
            try:
                self.vault.save(self.channel.account_id, self.password)
            except Exception:
                self.stage('操作成功，但系统未保存密码；下次登录需重新输入')

    def cleanup_credentials(self, error=None):
        if self.should_remove_account(error):
            try:
                self.vault.delete(self.channel.account_id)
            finally:
                self.auth.unlink(missing_ok=True)
                self.password = None
            return
        if error and '-20101' in str(error):
            try:
                self.vault.delete(self.channel.account_id)
            except Exception:
                pass
        if self.auth.exists():
            data = json.loads(self.auth.read_text(encoding='utf-8'))
            data.pop('password', None)
            # Legacy failure cleanup may remove the account too. Keep the registry identity.
            data['account'] = self.account
            duo_killer.write_private_json(self.auth, data)
        self.password = None

    def should_remove_account(self, error):
        from apple_login import LoginError
        cause = getattr(error, '__cause__', None)
        if (isinstance(error, requests.RequestException) or
                isinstance(cause, LoginError) and cause.status is not None
                and (cause.status == 429 or cause.status >= 500 and cause.status != 541)):
            return False
        return (self.action != 'enroll' and self.authentication_failed
                and isinstance(error, ValueError) and not self.submitted)

    def check_account(self, *, enrolling=False):
        if enrolling:
            self.stage('正在登录，完成验证后才会添加账号')
            duo_killer.login()
        with requests.Session() as session:
            duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
            save_session = True
            retried = False
            try:
                self.stage('正在查询当前登录人')
                try:
                    # Keep -me's account-page reauthentication and current SSI/session.
                    result = duo_killer.account_info(session)
                except (ValueError, requests.RequestException):
                    # Wrong passwords, rejected OTPs and a failed reauthentication must
                    # reach the user instead of starting another login loop.
                    if enrolling or self.authentication_requested:
                        raise
                    result = None
                if (not result or result.get('success') is not True
                        or result.get('logged_in') is not True or not result.get('apple_id')):
                    if enrolling or self.authentication_requested:
                        raise ValueError('登录后仍无法确认当前账号，请检查登录结果后重试。')
                    self.stage('未确认当前登录人，执行一次完整登录')
                    save_session = False
                    retried = True
                    duo_killer.login()
                    session.cookies.clear()
                    duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
                    save_session = True
                    result = duo_killer.account_info(session)
                if (result.get('success') is not True or result.get('logged_in') is not True
                        or not result.get('apple_id')):
                    raise ValueError('无法确认当前登录人，未启用此账号。')
                return result
            except ValueError:
                if not enrolling and (retried or self.authentication_requested):
                    self.authentication_failed = True
                raise
            finally:
                if save_session:
                    duo_killer.save_auth(session)

    def run(self, action, params):
        self.action = action
        self.channel.checkpoint()
        if action in ('preorder', 'preorder_resume', 'preorder_status'):
            import preorder
            if action == 'preorder_status':
                return preorder.PlanStore(self.root).public()
            result = preorder.run(self, params, resume=action == 'preorder_resume')
            self.persist_password()
            return result
        if action in ('purchase', 'checkout', 'add'):
            from preorder import guard_manual_purchase
            guard_manual_purchase(self.root)
        if action == 'address_read':
            return {'success': True, 'config': checkout_options.read_config(self.checkout)}
        if action == 'address_save':
            config = checkout_options.read_config(self.checkout)
            config.update(validate_address(params))
            checkout_options.save_config(self.checkout, config)
            return {'success': True, 'config': config, 'message': '快捷地址已保存，结账时可选择使用。'}
        if action == 'import_auth':
            data = validate_import(params.get('data'), self.account)
            duo_killer.write_private_json(self.auth, data)
            return {'success': True, 'message': '会话已导入，点击检查登录以确认有效性。'}
        if action == 'forget_password':
            self.vault.delete(self.channel.account_id)
            return {'success': True, 'message': '已删除系统保存的密码。'}
        if action == 'catalog':
            from shop_catalog import query_store, query_duo
            return query_duo(duo_killer.request_get) if params.get('mode') == 'duo' else query_store(duo_killer.request_get, phone_only=params.get('mode') == 'phone')
        self.ensure_auth_file()
        if action in ('enroll', 'me'):
            result = self.check_account(enrolling=action == 'enroll')
        elif action == 'login':
            result = duo_killer.login(sms=params.get('sms') is True)
        elif action == 'purchase':
            result = duo_killer.purchase_sku(params.get('sku', 'MK2U4CH'), quantity=quantity(params))
        elif action == 'add':
            result = duo_killer.add_to_bag(params.get('sku', ''), quantity=quantity(params), set_total=True)
        elif action == 'checkout':
            result = duo_killer.pay_bag()
        elif action == 'qr':
            source_id = str(params.get('source_task', ''))
            import uuid
            if str(uuid.UUID(source_id)) != source_id:
                raise ValueError('付款任务编号无效。')
            data = json.loads((self.root / 'tasks' / source_id / 'payment.json').read_text(encoding='utf-8'))
            duo_killer.payment_qr(data)
            duo_killer.write_private_json(self.task_dir / 'payment.json', data)
            result = data
        elif action in ('orders', 'bag', 'region'):
            if action == 'region':
                return {'success': True, 'region': checkout_options.current_region(self.auth)}
            with requests.Session() as session:
                duo_killer.authenticate(session, strong=True)
                try:
                    if action == 'orders':
                        result = duo_killer.order_list(session)
                    else:
                        flow = duo_killer.Checkout(session)
                        flow.page(duo_killer.BASE_URL + '/shop/bag', '读取购物袋')
                        result = {'success': True, 'items': [
                            {'name': duo_killer.order_text(duo_killer.field(item, 'd.name') or duo_killer.field(item, 'd.productName')),
                             'sku': duo_killer.field(item, 'd.sku'), 'quantity': duo_killer.field(item, 'itemQuantity.d.quantity'),
                             'subtotal': duo_killer.order_price(duo_killer.field(item, 'd.totalPrice'))}
                            for item in duo_killer.model_item_nodes(duo_killer.field(flow.model, 'shoppingCart.items', {}))],
                            'total': duo_killer.order_price(duo_killer.field(flow.model, 'shoppingCart.summary.d.total'))}
                finally:
                    duo_killer.save_auth(session)
        else:
            raise ValueError('不支持的操作。')
        self.persist_password()
        if action in ('enroll', 'login', 'me') and result.get('logged_in') is True:
            from apple_login import stored_device_trust
            result['device_trust_saved'] = bool(stored_device_trust(
                json.loads(self.auth.read_text(encoding='utf-8')), self.account))
        if result.get('qr_image'):
            path = Path(result['qr_image']).resolve()
            if path.parent != self.task_dir.resolve() or path.stat().st_size > 2_000_000:
                raise ValueError('二维码文件不在当前任务目录。')
            result['qr_data'] = 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()
        return result


def quantity(params):
    value = params.get('quantity', 1)
    if type(value) is not int or not 1 <= value <= 999:
        raise ValueError('数量须为 1–999 的整数；实际限购数量以官网为准。')
    return value


def validate_address(data):
    expected = {'contact': ['lastName', 'firstName', 'emailAddress', 'fullDaytimePhone'],
                'delivery': ['state', 'city', 'district'], 'address': ['street']}
    result = {}
    for group, keys in expected.items():
        values = data.get(group, {})
        result[group] = {}
        for key in keys:
            value = values.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError('请完整填写联系人、省市区和详细地址。')
            result[group][key] = value.strip()
    if not re.fullmatch(r'\d{11,12}', result['contact']['fullDaytimePhone']):
        raise ValueError('请输入正确的手机号码。')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['contact']['emailAddress']):
        raise ValueError('请输入正确的通知邮箱。')
    return result


def validate_import(data, account):
    if not isinstance(data, dict) or str(data.get('account', '')).casefold() != account.casefold():
        raise ValueError('导入文件的 account 必须与当前账号一致。')
    clean = empty_auth(account)
    groups = data.get('cookies', {})
    for group in clean['cookies']:
        rows = groups.get(group, [])
        if not isinstance(rows, list):
            raise ValueError('Cookie 分组格式不正确。')
        for cookie in rows:
            if not isinstance(cookie, dict) or not all(isinstance(cookie.get(k), str) for k in ('name', 'value', 'domain')):
                raise ValueError('Cookie 格式不正确。')
            domain = cookie['domain'].lstrip('.')
            if domain == 'apple.com.cn' or domain.endswith('.apple.com.cn'):
                clean['cookies'][group].append(cookie)
    clean['headers'] = {k: v for k, v in data.get('headers', {}).items()
                        if k.lower() in ('user-agent', 'accept-language') and isinstance(v, str)}
    from apple_login import stored_device_trust, TRUST_COOKIE_NAME
    clean['trust_account'] = account
    clean['trust_cookies'] = stored_device_trust(data, account)
    # Keep trust in one authoritative list, including when the source revoked it.
    for group, rows in clean['cookies'].items():
        clean['cookies'][group] = [c for c in rows if not TRUST_COOKIE_NAME.fullmatch(c['name'])]
    return clean
