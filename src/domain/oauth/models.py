from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class OAuthConnection:
    provider: str
    user_id: str
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: Optional[str] = None
    token_type: Optional[str] = None
