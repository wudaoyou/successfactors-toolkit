"""Temporary alias: the store moved to system_store (removed with TenantStore)."""

import sys

from successfactors_toolkit.services import system_store

sys.modules[__name__] = system_store
