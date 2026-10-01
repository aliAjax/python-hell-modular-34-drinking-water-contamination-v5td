class ChannelUnavailable(Exception):
    """渠道服务（短信/应急广播）不可用。"""


class InMemoryChannelGateway:
    """默认渠道网关：进程内实现，按幂等键去重发送。

    生产环境可替换为真实的短信/应急广播适配器；只要保留
    send(idempotency_key, ...) 语义，重试就不会产生重复外发。
    """

    def __init__(self):
        self.available = True
        self.attempts = []
        self._keys = set()
        self.fail_keys = set()

    def set_available(self, flag):
        self.available = bool(flag)

    def send(self, idempotency_key, channel, zone_id, message):
        if not self.available:
            raise ChannelUnavailable("渠道 %s 服务不可用" % channel)
        if idempotency_key in self.fail_keys:
            raise ChannelUnavailable("渠道 %s 拒绝 %s" % (channel, idempotency_key))
        record = {
            "idempotency_key": idempotency_key,
            "channel": channel,
            "zone_id": zone_id,
            "message": message,
            "duplicate": idempotency_key in self._keys,
        }
        self._keys.add(idempotency_key)
        self.attempts.append(record)
        return record
