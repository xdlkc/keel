from .errors import ConfirmationError


class InstanceActions:
    def __init__(self, config, cloud):
        self.config = config
        self.cloud = cloud

    def _confirm(self, confirmation):
        if not isinstance(confirmation, str) or confirmation != self.config.instance:
            raise ConfirmationError("确认实例名不匹配，未执行任何操作。")

    def start(self, confirmation):
        self._confirm(confirmation)
        self.cloud.start(self.config)

    def stop(self, confirmation):
        self._confirm(confirmation)
        self.cloud.stop(self.config)
