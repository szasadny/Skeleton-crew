from collections.abc import Collection, Iterable

from skeleton_crew.domain.trust_policy import (
    TRUSTED_PERMISSIONS,
    IssueComment,
    content_untampered,
    trusted_comments,
    trusted_label_time,
)
from skeleton_crew.github.gateway import Gateway, IssueRef


class _TrustResolver:
    def __init__(self, gw: Gateway, extra_trusted: Collection[str], bot_login: str | None) -> None:
        self._gw = gw
        self._known = {login.casefold(): True for login in extra_trusted}
        if bot_login:
            self._known[bot_login.casefold()] = True

    async def resolve(self, logins: Iterable[str]) -> None:
        for login in logins:
            key = login.casefold()
            if key in self._known:
                continue
            if not login or login.endswith("[bot]"):
                self._known[key] = False
                continue
            try:
                role = await self._gw.permission_of(login)
            except Exception:
                role = ""
            self._known[key] = role in TRUSTED_PERMISSIONS

    def is_trusted(self, login: str) -> bool:
        return self._known.get(login.casefold(), False)


async def trusted_issues(
    gw: Gateway, label: str, extra_trusted: Collection[str], bot_login: str | None
) -> list[tuple[IssueRef, list[IssueComment]]]:
    resolver = _TrustResolver(gw, extra_trusted, bot_login)
    result: list[tuple[IssueRef, list[IssueComment]]] = []
    for issue in await gw.list_open_issues(label):
        data = await gw.trust_data(issue.number)
        await resolver.resolve(
            e.actor for e in data.label_events if e.label.casefold() == label.casefold()
        )
        since = trusted_label_time(data.label_events, label, resolver.is_trusted)
        if since is None:
            continue
        await resolver.resolve(e.editor for e in data.edits if e.at >= since)
        if not content_untampered(data.edits, since, resolver.is_trusted):
            continue
        await resolver.resolve(c.author for c in data.comments)
        result.append((issue, trusted_comments(data.comments, resolver.is_trusted)))
    return result
