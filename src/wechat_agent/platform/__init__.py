"""Operating-system adapters, separate from public-platform connectors."""

def create_backend(settings):
    if settings.effective_platform == "linux":
        from .linux.backend import LinuxBackend
        return LinuxBackend(settings)
    from ..weixin_ui import WeixinUiAdapter
    return WeixinUiAdapter(settings)
