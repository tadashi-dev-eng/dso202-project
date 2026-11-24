"""Caller identity inside the cluster.

Only api-gateway validates JWTs. After validating, it forwards the request
with ``X-User-Id`` set from the token's ``sub`` claim (any client-supplied
``X-User-Id`` is stripped first). Backend services trust that header; the
default-deny NetworkPolicies make the gateway, plus the few documented
service-to-service paths, the only way to reach them.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

USER_HEADER = "x-user-id"


def current_user(request: Request) -> str:
    user_id = request.headers.get(USER_HEADER)
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user identity")
    return user_id


CurrentUser = Annotated[str, Depends(current_user)]
