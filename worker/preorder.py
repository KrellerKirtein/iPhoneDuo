"""账号级定时购买计划；提交结果不明时只查单，绝不重新提交。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import uuid

import requests

import checkout_options as options
import duo_killer as shop


SEALED = {'submitting', 'uncertain', 'confirmed'}


class Unavailable(ValueError):
    """官网明确没有当前计划可用的履约选项，可以有限等待后重新读取。"""


def money(value):
    """将明确的人民币金额转换为分；拒绝未知币种、非正数和非有限数。"""
    text = str(value).strip()
    match = re.fullmatch(r'(?:RMB|CNY|CN¥|¥|￥)?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?:元|RMB|CNY)?', text)
    if not match:
        raise ValueError('必须提供明确的人民币金额。')
    try:
        amount = Decimal(match[1].replace(',', ''))
    except InvalidOperation:
        raise ValueError('金额格式无效。') from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError('金额必须大于零。')
    return int(amount * 100)


def parse_time(value):
    """显式时区是计划契约的一部分，不使用机器本地时区推断开售时间。"""
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('开售时间格式无效。') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('开售时间必须包含时区，例如 +08:00。')
    return result.astimezone(timezone.utc)


def validate_plan(data):
    """冻结用户授权的 SKU、数量、金额和门店范围。"""
    if not isinstance(data, dict) or not isinstance(data.get('sku'), str):
        raise ValueError('购买计划和商品 SKU 格式无效。')
    if data.get('authorized') is not True:
        raise ValueError('请先授权按计划自动创建订单；程序不会付款。')
    target = parse_time(data.get('target_at'))
    quantity = data.get('quantity', 1)
    if type(quantity) is not int or quantity != 1:
        raise ValueError('定时计划每次只购买 1 件商品。')
    duration = data.get('window_seconds', 300)
    if type(duration) is not int or not 30 <= duration <= 900:
        raise ValueError('执行窗口须为 30–900 秒。')
    stores = data.get('pickup_stores', [])
    if (not isinstance(stores, list) or len(stores) > 10 or
            any(not isinstance(s, str) or not re.fullmatch(r'R\d{3,5}', s) for s in stores)):
        raise ValueError('备选门店须为 R 开头的官网门店编号，最多 10 家。')
    suffix = data.get('id_suffix', '') if stores else ''
    if stores and (not isinstance(suffix, str) or not re.fullmatch(r'[0-9A-Za-z]{4}', suffix)):
        raise ValueError('启用取货备选须填写取货人证件后四位。')
    return {'sku': shop.normalize_sku(data.get('sku', '')), 'quantity': quantity,
            'target_at': target.isoformat(), 'window_seconds': duration,
            'max_total_cents': money(data.get('max_total', '')),
            'pickup_stores': list(dict.fromkeys(stores)), 'id_suffix': suffix.upper(),
            'authorized': True}


class PlanStore:
    """调用方必须持有账号文件锁；每次状态转移原子持久化后才允许外部动作。"""
    def __init__(self, root):
        self.path = root / 'preorder.json'
        self.data = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(self.data, dict) or self.data.get('version') != 1 or not isinstance(self.data.get('plan'), dict)
                    or self.data.get('state') not in {'prepared', 'waiting', 'running', 'paused', 'expired', *SEALED}):
                raise ValueError('购买计划记录无法识别，请保留文件并先核对官网订单。')

    def save(self, **changes):
        """把状态及关联证据一起原子落盘，写入异常直接阻断后续业务请求。"""
        self.data = {**self.data, **changes, 'updated_at': datetime.now(timezone.utc).isoformat()}
        shop.write_private_json(self.path, self.data)

    def public(self):
        """不向状态快照返回地址、证件或支付凭据。"""
        if not self.data:
            return {'success': True, 'message': '尚未保存定时购买计划。'}
        plan = self.data['plan']
        return {'success': True, 'status': 'preorder_plan', 'state': self.data['state'],
                'target_at': plan['target_at'], 'sku': plan['sku'],
                'order_number': self.data.get('result', {}).get('order_number'),
                'message': self.data.get('message', '')}


def guard_manual_purchase(root):
    """普通购买不能绕过自动计划仍待确认的提交意图。"""
    store = PlanStore(root)
    if store.data.get('state') in SEALED:
        raise ValueError('已有计划提交记录，请先查询官网订单；已阻止再次购买。')


class Policy:
    """自动模式只在完整匹配预设时替代人工决定，未知输入停止而非默认同意。"""
    def __init__(self, service, store, config_path):
        self.service, self.store, self.config_path = service, store, config_path
        self.plan = store.data['plan']
        self.review = None
        self.mode = 'HOME'
        self.store_id = None
        self.store_name = None
        self.deadline = parse_time(self.plan['target_at']) + timedelta(seconds=self.plan['window_seconds'])

    def before_action(self):
        self.service.channel.checkpoint()
        if self.store.data['state'] not in SEALED and datetime.now(timezone.utc) >= self.deadline:
            raise ValueError('执行窗口已结束，未提交订单。')

    def confirm(self, details):
        """核对整个购物袋、送货街道或门店及金额；省市区在地址步骤按快照提交。"""
        items = details.get('items', [])
        if (len(items) != 1 or shop.normalize_sku(items[0].get('sku', '')) != self.plan['sku']
                or items[0].get('quantity') != self.plan['quantity']):
            raise ValueError('订单商品或数量与购买计划不同，未自动提交。')
        if money(details.get('total', '')) > self.plan['max_total_cents']:
            raise ValueError('订单金额超过计划上限，未自动提交。')
        if not items[0].get('name'):
            raise ValueError('订单商品名称缺失，无法在超时后核对订单。')
        if self.mode == 'HOME':
            street = self.store.data['address']['address']['street']
            if re.sub(r'\s+', '', details.get('address', '')) != re.sub(r'\s+', '', street):
                raise ValueError('订单复核页送货地址与计划不同或缺失，未自动提交。')
        elif (not self.store_name or shop.order_text(details.get('store')) != self.store_name
              or self.store_id not in self.plan['pickup_stores']):
            raise ValueError('订单复核页未确认备选取货门店。')
        self.review = details
        return True

    def before_submit(self):
        """先持久化提交意图；落盘失败时禁止发送订单创建请求。"""
        self.before_action()
        if not self.review or self.store.data['state'] in SEALED:
            raise ValueError('购买计划已提交或尚未核对，禁止再次提交。')
        self.store.save(state='submitting', review=self.review,
                        submitted_at=datetime.now(timezone.utc).isoformat(),
                        message='正在提交订单；结果不明时只查单。')
        self.service.submitted = True

    def validate_result(self, result):
        """待付款成功必须具有订单号，且金额与提交前复核完全一致。"""
        if not re.fullmatch(r'W\d{6,}', str(result.get('order_number', ''))):
            raise ValueError('未获得有效订单号，需要查询订单确认。')
        if money(result.get('amount_due', '')) != money(self.review['total']):
            raise ValueError('订单返回金额与复核金额不同，需要查询订单确认。')

    def fulfillment(self, flow):
        """仅在官网明确不提供送货选项时，按白名单顺序选取货门店。"""
        config = options.read_config(self.config_path)
        path = 'checkout.fulfillment'
        node = flow.require(path + '.fulfillmentOptions')['d']
        choices = node.get('options')
        if not isinstance(choices, list) or not choices:
            raise ValueError('收货方式响应不完整，未推断为无货。')
        available = {o['value'] for o in choices if o.get('value') in ('HOME', 'RETAIL') and not o.get('disabled')}
        if 'HOME' in available:
            try:
                options.home_delivery(flow, config)
                self.mode = 'HOME'
                return
            except Unavailable:
                # 仅配送服务明确为空时切换；网络和未知模型不会走此分支。
                pass
        if 'RETAIL' not in available or not self.plan['pickup_stores']:
            raise Unavailable('送货暂不可用，且没有可用的取货备选。')
        selector = path + '.fulfillmentOptions'
        payload = {selector + '.selectFulfillmentLocation': 'RETAIL'}
        flow.action(selector, 'selectFulfillmentLocationAction', payload)
        if shop.field(flow.model, selector + '.d.selectFulfillmentLocation') != 'RETAIL':
            raise ValueError('官网未确认切换为取货。')
        locator = path + '.pickupTab.pickup.storeLocator'
        node = flow.require(locator)
        region = locator + '.address.stateCitySelectorForCheckout'
        store_data = {locator + '.showAllStores': 'true', locator + '.selectStore': '',
                      locator + '.searchInput': config['delivery']['city']}
        store_data.update({region + '.' + k: shop.field(node, 'address.stateCitySelectorForCheckout.d.' + k, '')
                           for k in ('state', 'city', 'district', 'provinceCityDistrict', 'countryCode')})
        flow.action(locator, 'search', store_data)
        stores = shop.field(flow.model, locator + '.searchResults.d.retailStores')
        if not isinstance(stores, list):
            raise ValueError('取货门店响应不完整。')
        candidates = [s for wanted in self.plan['pickup_stores'] for s in stores
                      if s.get('storeId') == wanted and s.get('storeDisabled') is False]
        for selected in candidates:
            self.store_id = selected['storeId']
            self.store_name = shop.order_text(selected.get('storeName'))
            if not self.store_name:
                raise ValueError('门店名称缺失，无法核对订单。')
            store_data[locator + '.selectStore'] = self.store_id
            flow.action(locator, 'select', store_data)
            if shop.field(flow.model, locator + '.d.selectStore') != self.store_id:
                raise ValueError('官网未确认所选取货门店。')
            if shop.field(flow.model, path + '.pickupTab.deliveryForced.shipmentGroups'):
                raise ValueError('自动计划不支持混合送货与取货订单。')
            try:
                slot_data = pickup_slot(flow.model)
                break
            except Unavailable:
                continue
        else:
            raise Unavailable('备选门店暂无可选库存或可预约时段。')
        flow.action(path, 'continueFromFulfillmentToPickupContact', {**payload, **store_data, **slot_data})
        flow.require('checkout.pickupContact')
        self.mode = 'RETAIL'
        options.pickup_contact(flow, config, self.config_path)


class PlanCheckout(shop.Checkout):
    """复用已有页面协议，在每次业务请求前检查取消与执行窗口。"""
    def __init__(self, session, policy):
        super().__init__(session)
        self.policy = policy

    def action(self, module, name, data=None):
        self.policy.before_action()
        return super().action(module, name, data)

    def page(self, url, stage):
        self.policy.before_action()
        return super().page(url, stage)


def pickup_slot(model):
    """按官网顺序选择首个非受限时段；日期、签名均取当前响应，不补造字段。"""
    base = 'checkout.fulfillment.pickupTab.pickup.timeSlot'
    if not shop.field(model, base):
        return {}
    path = base + '.dateTimeSlots'
    node = shop.field(model, path + '.d', {})
    windows, dates = node.get('timeSlotWindows'), node.get('pickUpDates')
    if not isinstance(windows, list) or not isinstance(dates, list) or len(windows) != len(dates):
        raise ValueError('取货预约时段结构无法识别，未提交订单。')
    for index, window in enumerate(windows):
        date = dates[index].get('date')
        try:
            day = datetime.strptime(date, '%Y-%m-%d').day
        except (TypeError, ValueError):
            raise ValueError('取货日期无法识别，未推断日期。') from None
        if not isinstance(window, dict) or len(window) != 1:
            raise ValueError('取货时段日期分组无法识别。')
        day_radio, slots = next(iter(window.items()))
        if not str(day_radio).isdigit() or int(day_radio) != day or not isinstance(slots, list):
            raise ValueError('取货时段与日期不匹配。')
        for slot in slots:
            if slot.get('disabled') or slot.get('isRestricted') in (True, 'true'):
                continue
            mapping = {'startTime': 'checkInStart', 'endTime': 'checkInEnd', 'timeSlotId': 'SlotId',
                       'signKey': 'signKey', 'timeZone': 'timeZone', 'timeSlotValue': 'timeSlotValue'}
            if any(not isinstance(slot.get(key), str) or not slot[key] for key in mapping.values()):
                raise ValueError('取货时段缺少时间、编号或签名，未提交订单。')
            payload = {path + '.' + key: slot[source] for key, source in mapping.items()}
            payload.update({path + '.date': date, path + '.dayRadio': str(day_radio),
                            path + '.displayStartTime': '', path + '.displayEndTime': '',
                            path + '.isRecommended': 'false', path + '.isRestricted': ''})
            return payload
    raise Unavailable('备选门店暂时没有可预约的取货时段。')


def wait_until(service, target, now=None, pause=None):
    """以绝对时刻校准、短等待响应取消；机器唤醒后重新检查截止时间。"""
    now = now or (lambda: datetime.now(timezone.utc))
    pause = pause or service.channel.cancelled.wait
    while True:
        service.channel.checkpoint()
        remaining = (target - now()).total_seconds()
        if remaining <= 0:
            return
        pause(min(remaining, 0.5))


def reconcile(service, store, session):
    """只读查单，基线之外唯一完整匹配才确认；空列表不解除防重保护。"""
    if store.data['state'] == 'confirmed':
        return store.data['result']
    review = store.data.get('review', {})
    expected = sorted((shop.order_text(i.get('name')), i.get('quantity')) for i in review.get('items', []))
    baseline = set(store.data.get('baseline', []))
    for attempt in range(4):
        service.channel.checkpoint()
        try:
            listing = shop.order_list(session)
            if (listing.get('success') is not True or listing.get('has_more') is not False
                    or store.data.get('baseline_complete') is not True):
                raise ValueError('订单列表不完整。')
            candidates = [o for o in listing.get('orders', [])
                          if o.get('order_number') not in baseline and expected and
                          sorted((shop.order_text(i.get('name')), i.get('quantity')) for i in o.get('items', [])) == expected]
            if len(candidates) == 1 and re.fullmatch(r'W\d{6,}', candidates[0]['order_number']):
                result = {'success': True, 'status': 'order_confirmed',
                          'order_number': candidates[0]['order_number'], 'payment_status': 'unknown',
                          'message': '订单列表已确认新增的匹配订单，已停止购买；请在官网核对付款状态。'}
                store.save(state='confirmed', result=result, message=result['message'])
                return result
            if len(candidates) > 1:
                break
        except (ValueError, requests.RequestException):
            pass
        if attempt < 3:
            service.channel.cancelled.wait(2)
    result = {'success': False, 'status': 'order_uncertain',
              'message': '订单提交结果尚未确认，已阻止重复下单。请查询官网订单或再次恢复查单。'}
    store.save(state='uncertain', message=result['message'])
    return result


def run(service, params, *, resume=False):
    """执行或恢复单账号购买计划；任何提交意图都先恢复结果而不是重发购买。"""
    store = PlanStore(service.root)
    if store.data.get('state') == 'confirmed':
        service.submitted = True
        if store.data['result'].get('status') == 'pending_payment':
            shop.write_private_json(service.task_dir / 'payment.json', store.data['result'])
        return store.data['result']
    if not resume and store.data.get('state') not in SEALED:
        from service import validate_address
        plan = validate_plan(params)
        address = validate_address(options.read_config(service.checkout))
        store.data = {'version': 1, 'id': str(uuid.uuid4()), 'plan': plan, 'address': address}
        store.save(state='prepared', message='计划已保存。')
    elif not store.data:
        raise ValueError('尚未保存可恢复的购买计划。')
    sealed = store.data['state'] in SEALED
    service.submitted = sealed
    service.ensure_auth_file()
    with requests.Session() as session:
        shop.load_auth(session, json.loads(service.auth.read_text(encoding='utf-8')))
        try:
            if sealed:
                service.stage('恢复订单提交结果，仅查询订单', order_submitted=True)
                return reconcile(service, store, session)
            target = parse_time(store.data['plan']['target_at'])
            deadline = target + timedelta(seconds=store.data['plan']['window_seconds'])
            if datetime.now(timezone.utc) >= deadline:
                store.save(state='expired', message='计划已超过执行窗口，未补发订单。')
                return store.public()
            service.stage('预购准备：检查登录，必要时请完成验证')
            shop.ensure_strong_login(session)
            store.save(state='waiting', message='等待计划时间，电脑须保持唤醒。')
            service.stage('等待北京时间 ' + target.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S') + '，开售前检查会话')
            wait_until(service, target - timedelta(seconds=30))
            if datetime.now(timezone.utc) >= deadline:
                store.save(state='expired', message='唤醒时已超过执行窗口，未补发订单。')
                return store.public()
            shop.ensure_strong_login(session)
            for attempt in range(3):
                try:
                    baseline = shop.order_list(session)
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    service.channel.cancelled.wait(2)
                    service.channel.checkpoint()
            if baseline.get('success') is not True or type(baseline.get('has_more')) is not bool:
                raise ValueError('未取得有效订单列表，不能建立防重基线。')
            store.save(baseline=[o['order_number'] for o in baseline['orders']],
                       baseline_complete=not baseline['has_more'])
            service.stage('预检完成，等待开售；不会提前加购或下单')
            wait_until(service, target)
            store.save(state='running')
            config_path = service.task_dir / 'preorder-address.json'
            shop.write_private_json(config_path, store.data['address'])
            policy = Policy(service, store, config_path)
            service.preorder_policy = policy
            failures = 0
            while datetime.now(timezone.utc) < deadline:
                service.channel.checkpoint()
                try:
                    flow = PlanCheckout(session, policy)
                    flow.page(shop.BASE_URL + '/shop/bag', '读取预购购物袋')
                    if not shop.check_login(session, page=flow.page_response)['logged_in']:
                        raise ValueError('开售时会话已失效，请重新登录后恢复计划。')
                    items = shop.model_items(shop.field(flow.model, 'shoppingCart.items', {}))
                    if any(sku.upper() != policy.plan['sku'] for sku, _ in items):
                        raise ValueError('购物袋包含计划以外的商品，请先人工处理。')
                    shop.add_to_bag(policy.plan['sku'], quantity=1, set_total=True, flow=flow)
                    result = shop.pay_bag(flow=flow)
                    policy.validate_result(result)
                    store.save(state='confirmed', result=result, message='订单已创建，等待付款。')
                    return result
                except (requests.RequestException, shop.CartNotReady, shop.TemporaryCheckoutError, Unavailable):
                    if store.data['state'] in SEALED:
                        return reconcile(service, store, session)
                    failures += 1
                    service.stage('尚未完成加购或履约，读取当前状态后再试')
                    remaining = max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
                    service.channel.cancelled.wait(min(2 ** min(failures, 4), 10, remaining))
                except Exception:
                    if store.data['state'] in SEALED:
                        return reconcile(service, store, session)
                    raise
            store.save(state='expired', message='执行窗口结束，未创建订单。')
            return store.public()
        except BaseException:
            # 包括取消和 KeyboardInterrupt；断电仍由事先落盘的 submitting 保护。
            if store.data['state'] not in SEALED:
                store.save(state='paused', message='计划已暂停，可在执行窗口内恢复。')
            raise
        finally:
            service.preorder_policy = None
            shop.save_auth(session)
