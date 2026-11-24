import logging
from collections.abc import Collection, Iterable

from skeleton_crew.domain.label_policy import same_label
from skeleton_crew.domain.trust_policy import (
    IssueComment,
    always_trusted,
    content_untampered,
    never_trusted,
    trusted_comments,
    trusted_label_time,
    trusted_role,
)
from skeleton_crew.github.gateway import Gateway, IssueRef

log = logging.getLogger(__name__)


class _TrustResolver:
    def __init__(self, gw: Gateway, extra_trusted: Collection[str], bot_login: str | None) -> None:
        self._gw = gw
        self._known = dict.fromkeys(always_trusted(extra_trusted, bot_login), True)

    async def resolve(self, logins: Iterable[str]) -> None:
        for login in logins:
            key = login.casefold()
            if key in self._known:
                continue
            if never_trusted(login):
                self._known[key] = False
                continue
            try:
                role = await self._gw.permission_of(login)
            except Exception as exc:
                reason = type(exc).__name__
                log.warning("role of %s unreadable (%s), treated as untrusted", login, reason)
                role = ""
            self._known[key] = trusted_role(role)

    def is_trusted(self, login: str) -> bool:
        return self._known.get(login.casefold(), False)


async def trusted_issues(
    gw: Gateway, label: str, extra_trusted: Collection[str], bot_login: str | None
) -> list[tuple[IssueRef, list[IssueComment]]]:
    resolver = _TrustResolver(gw, extra_trusted, bot_login)
    result: list[tuple[IssueRef, list[IssueComment]]] = []
    for issue in await gw.list_open_issues(label):
        data = await gw.trust_data(issue.number)
        await resolver.resolve(e.actor for e in data.label_events if same_label(e.label, label))
        since = trusted_label_time(data.label_events, label, resolver.is_trusted)
        if since is None:
            continue
        await resolver.resolve(e.editor for e in data.edits if e.at >= since)
        if not content_untampered(data.edits, since, resolver.is_trusted):
            continue
        await resolver.resolve(c.author for c in data.comments)
        result.append((issue, trusted_comments(data.comments, resolver.is_trusted)))
    return result
