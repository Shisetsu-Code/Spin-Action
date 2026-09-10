from tester_spin.providers.rubyplay.launcher_params import (
    install_runtime_launcher_parser,
)
from tester_spin.providers.rubyplay.launcher_resolver import (
    install_runtime_launcher_resolver,
)

install_runtime_launcher_parser()
install_runtime_launcher_resolver()

from tester_spin.providers.rubyplay.adapter import RubyPlayProvider

__all__ = ["RubyPlayProvider"]
