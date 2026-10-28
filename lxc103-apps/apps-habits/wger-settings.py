# Settings do wger com nomes de cookie próprios. Várias apps em *.ihome.dev.br são Django e usam
# "sessionid"/"csrftoken"; com um cookie desses no domínio pai, o navegador manda dois com o mesmo
# nome e o wger lê o errado (o login dá 302 e a página seguinte volta deslogada).
from .main import *  # noqa: F401,F403

SESSION_COOKIE_NAME = 'wger_sessionid'
CSRF_COOKIE_NAME = 'wger_csrftoken'
LANGUAGE_COOKIE_NAME = 'wger_language'
