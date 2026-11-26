"""Explicit staging-only wiring for an existing Flask host; disabled by default."""
import re

ROUTES = {'/api/v3/market/quotes','/api/v3/fund/performance','/v3/fund'}


def attach_staging(app, *, mode=None, market=None, ledger=None, token=None):
    """No environment reads, startup tasks, DB migrations, or production mode.
    Host injects isolated dependencies. Exposes read-only routes, not fill routes.
    """
    if mode in (None,'off'):
        return False
    if mode!='staging-readonly':
        raise ValueError('V3 production activation not supported')
    if market is None or ledger is None or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token) or len(set(token))<16:
        raise ValueError('missing isolated dependencies or invalid token')
    if ROUTES.intersection(r.rule for r in app.url_map.iter_rules()):
        raise ValueError('V3 route collision')
    ledger.read() # require existing schema; do not initialize or change anything
    from v3_management import register_host
    register_host(app,market=market,ledger=ledger,token=token)
    return True


def cached_quotes(market, codes):
    """Translate authenticated in-process market cache into fund quote format."""
    codes=tuple(sorted(set(codes)))
    if not codes: return {}
    payload,status=market.read(codes)
    if status!=200 or payload.get('complete') is not True or set(payload['quotes'])!=set(codes):
        raise ValueError('incomplete cached quotes')
    return {c:dict(row,code=c) for c,row in payload['quotes'].items()}
