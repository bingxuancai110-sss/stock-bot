"""Authenticate only the stock-bot main reminder workflow, not arbitrary Actions."""
import hmac
import jwt

REPOSITORY = "bingxuancai110-sss/stock-bot"
AUDIENCE = "https://stock-bot-6xct.onrender.com/cron/taiex-reduction"
ISSUER = "https://token.actions.githubusercontent.com"
WORKFLOW = REPOSITORY + "/.github/workflows/taiex-reduction.yml@refs/heads/main"
_keys = jwt.PyJWKClient(ISSUER + "/.well-known/jwks", timeout=5, lifespan=300)


def authorized(headers, token, secret, keys=None, *, audience=AUDIENCE, workflow=WORKFLOW):
    bearer = headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        raw = bearer[7:]
        if not raw or len(raw) > 16000:
            return False
        try:
            if jwt.get_unverified_header(raw).get("alg") != "RS256":
                return False
            key = (keys or _keys).get_signing_key_from_jwt(raw).key
            claims = jwt.decode(raw, key, algorithms=["RS256"], audience=audience,
                issuer=ISSUER, leeway=20,
                options={"require": ["exp", "iat", "nbf", "repository", "repository_id", "ref", "workflow_ref", "event_name"]})
            return (claims["repository"] == REPOSITORY and str(claims["repository_id"]) == "1327818651"
                    and claims["ref"] == "refs/heads/main" and claims["workflow_ref"] == workflow
                    and claims["event_name"] in {"schedule", "workflow_dispatch", "push"}
                    and 0 < claims["exp"] - claims["iat"] <= 600)
        except (jwt.PyJWTError, ValueError, TypeError):
            return False
    return bool(secret and token and hmac.compare_digest(str(secret), str(token)))
