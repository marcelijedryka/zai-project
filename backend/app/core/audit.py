"""Audit trail for administrative actions: one structured INFO line per successful change.

Reads (and rejected attempts) are already covered by the access log, so only completed
changes are recorded here: who (``adminId``), what (``action``) and on which resource.
"""

import logging
from typing import Any

audit_logger = logging.getLogger("zai.audit")


def audit(action: str, admin_id: int, **fields: Any) -> None:
    audit_logger.info(action, extra={"fields": {"adminId": admin_id, **fields}})
