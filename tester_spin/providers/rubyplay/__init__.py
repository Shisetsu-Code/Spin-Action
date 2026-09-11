from tester_spin.providers.rubyplay.launcher_params import (
    install_runtime_launcher_parser,
)
from tester_spin.providers.rubyplay.launcher_resolver import (
    install_runtime_launcher_resolver,
)

install_runtime_launcher_parser()
install_runtime_launcher_resolver()

from tester_spin.providers.rubyplay.adapter import RubyPlayProvider
from tester_spin.providers.rubyplay.runtime_contracts import install_runtime_contracts
from tester_spin.providers.rubyplay.bootstrap_fallback import install_bootstrap_fallback
from tester_spin.providers.rubyplay.availability import install_availability_classification

install_runtime_contracts()
install_bootstrap_fallback()

# Import after the previous installers so this layer wraps the final discovery /
# bootstrap chain instead of bypassing runtime_contracts or bootstrap_fallback.
from tester_spin.providers.rubyplay.client_capability import install_client_capability

install_client_capability()
install_availability_classification()

__all__ = ["RubyPlayProvider"]
