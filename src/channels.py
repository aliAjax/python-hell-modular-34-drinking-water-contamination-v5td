from .repository import now_iso

CHANNELS = ("sms", "broadcast")


class ChannelGateway:
    """模拟短信和应急广播渠道服务。

    渠道可用性持久化在 SQLite 中，服务重启后仍然有效；渠道不可用时投递保持
    pending，恢复后由 resume 接着处理，已确认的成功回执不会重发。
    """

    def __init__(self, repository):
        self.repository = repository

    def is_available(self, channel):
        return self.repository.is_channel_available(channel)

    def set_available(self, channel, available):
        if channel not in CHANNELS:
            from .domain import DomainError
            raise DomainError("unknown_channel", "不支持的渠道: %s" % channel, 400)
        self.repository.set_channel_available(channel, bool(available))

    def status(self):
        return {channel: self.is_available(channel) for channel in CHANNELS}

    def send(self, channel, notice, zone_id):
        """尝试投递，返回回执字段。渠道不可用时返回 pending 且不产生成功回执。"""
        if not self.is_available(channel):
            return {"status": "pending", "channel_time": None, "error": "channel_unavailable"}
        return {"status": "success", "channel_time": now_iso(), "error": None}
