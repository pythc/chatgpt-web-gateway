import ast
import os

from dotenv import load_dotenv

from utils.Logger import logger

load_dotenv(encoding="ascii")


def is_true(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.lower() in ['true', '1', 't', 'y', 'yes']
    elif isinstance(x, int):
        return x == 1
    else:
        return False


api_prefix = os.getenv('API_PREFIX', None)
default_model = os.getenv('DEFAULT_MODEL', 'auto')
default_reasoning_effort = os.getenv('DEFAULT_REASONING_EFFORT', 'high')
default_tool_max_tokens = int(os.getenv('DEFAULT_TOOL_MAX_TOKENS', '262144'))
if default_tool_max_tokens < 1:
    raise ValueError('DEFAULT_TOOL_MAX_TOKENS must be positive')
conversation_timeout = int(os.getenv('CONVERSATION_TIMEOUT', '3600'))
recovery_max_reads = int(os.getenv('RECOVERY_MAX_READS', '120'))
recovery_max_seconds = int(os.getenv('RECOVERY_MAX_SECONDS', '600'))
websocket_idle_timeout = int(os.getenv('WEBSOCKET_IDLE_TIMEOUT', '3600'))
websocket_handshake_max_attempts = int(os.getenv('WEBSOCKET_HANDSHAKE_MAX_ATTEMPTS', '3'))
websocket_receive_max_reconnects = int(os.getenv('WEBSOCKET_RECEIVE_MAX_RECONNECTS', '5'))
if websocket_idle_timeout < 0 or websocket_handshake_max_attempts < 1 or websocket_receive_max_reconnects < 0:
    raise ValueError('Invalid websocket recovery limits')
auto_continue = is_true(os.getenv('AUTO_CONTINUE', 'true'))
auto_continue_max_rounds = int(os.getenv('AUTO_CONTINUE_MAX_ROUNDS', '64'))
auto_continue_max_tokens = int(os.getenv('AUTO_CONTINUE_MAX_TOKENS', '262144'))
auto_continue_timeout = int(os.getenv('AUTO_CONTINUE_TIMEOUT', '3600'))
if auto_continue_max_rounds < 0 or auto_continue_max_tokens < 1 or auto_continue_timeout < 1 or recovery_max_reads < 0 or recovery_max_seconds < 0:
    raise ValueError('Invalid automatic continuation limits')
authorization = os.getenv('AUTHORIZATION', '').replace(' ', '')
chatgpt_base_url = os.getenv('CHATGPT_BASE_URL', 'https://chatgpt.com').replace(' ', '')
auth_key = os.getenv('AUTH_KEY', None)
x_sign = os.getenv('X_SIGN', None)

ark0se_token_url = os.getenv('ARK' + 'OSE_TOKEN_URL', '').replace(' ', '')
if not ark0se_token_url:
    ark0se_token_url = os.getenv('ARK0SE_TOKEN_URL', None)
proxy_url = os.getenv('PROXY_URL', '').replace(' ', '')
sentinel_proxy_url = os.getenv('SENTINEL_PROXY_URL', None)
export_proxy_url = os.getenv('EXPORT_PROXY_URL', None)
file_host = os.getenv('FILE_HOST', None)
voice_host = os.getenv('VOICE_HOST', None)
impersonate_list_str = os.getenv('IMPERSONATE', '[]')
user_agents_list_str = os.getenv('USER_AGENTS', '[]')
device_tuple_str = os.getenv('DEVICE_TUPLE', '()')
browser_tuple_str = os.getenv('BROWSER_TUPLE', '()')
platform_tuple_str = os.getenv('PLATFORM_TUPLE', '()')

cf_file_url = os.getenv('CF_FILE_URL', None)
turnstile_solver_url = os.getenv('TURNSTILE_SOLVER_URL', None)

history_disabled = is_true(os.getenv('HISTORY_DISABLED', True))
pow_difficulty = os.getenv('POW_DIFFICULTY', '000032')
retry_times = int(os.getenv('RETRY_TIMES', 3))
conversation_only = is_true(os.getenv('CONVERSATION_ONLY', False))
enable_limit = is_true(os.getenv('ENABLE_LIMIT', True))
upload_by_url = is_true(os.getenv('UPLOAD_BY_URL', False))
check_model = is_true(os.getenv('CHECK_MODEL', False))
scheduled_refresh = is_true(os.getenv('SCHEDULED_REFRESH', False))
random_token = is_true(os.getenv('RANDOM_TOKEN', True))
oai_language = os.getenv('OAI_LANGUAGE', 'zh-CN')

authorization_list = authorization.split(',') if authorization else []
chatgpt_base_url_list = chatgpt_base_url.split(',') if chatgpt_base_url else []
ark0se_token_url_list = ark0se_token_url.split(',') if ark0se_token_url else []
proxy_url_list = proxy_url.split(',') if proxy_url else []
sentinel_proxy_url_list = sentinel_proxy_url.split(',') if sentinel_proxy_url else []
impersonate_list = ast.literal_eval(impersonate_list_str)
user_agents_list = ast.literal_eval(user_agents_list_str)
device_tuple = ast.literal_eval(device_tuple_str)
browser_tuple = ast.literal_eval(browser_tuple_str)
platform_tuple = ast.literal_eval(platform_tuple_str)

enable_gateway = is_true(os.getenv('ENABLE_GATEWAY', False))
auto_seed = is_true(os.getenv('AUTO_SEED', True))
force_no_history = is_true(os.getenv('FORCE_NO_HISTORY', False))
no_sentinel = is_true(os.getenv('NO_SENTINEL', False))

with open('version.txt') as f:
    version = f.read().strip()

logger.info("-" * 60)
logger.info(f"Chat2Api {version} | https://github.com/lanqian528/chat2api")
logger.info("-" * 60)
logger.info("Environment variables:")
logger.info("------------------------- Security -------------------------")
logger.info("API_PREFIX:        " + str(api_prefix))
logger.info("AUTHORIZATION:     " + ("[configured]" if authorization_list else "[not configured]"))
logger.info("AUTH_KEY:          " + ("[configured]" if auth_key else "[not configured]"))
logger.info("------------------------- Request --------------------------")
logger.info("CHATGPT_BASE_URL:  " + str(chatgpt_base_url_list))
logger.info("PROXY_URL:         " + str(proxy_url_list))
logger.info("EXPORT_PROXY_URL:  " + str(export_proxy_url))
logger.info("FILE_HOST:     " + str(file_host))
logger.info("VOICE_HOST:    " + str(voice_host))
logger.info("IMPERSONATE:       " + str(impersonate_list))
logger.info("USER_AGENTS:       " + str(user_agents_list))
logger.info("---------------------- Functionality -----------------------")
logger.info("HISTORY_DISABLED:  " + str(history_disabled))
logger.info("POW_DIFFICULTY:    " + str(pow_difficulty))
logger.info("RETRY_TIMES:       " + str(retry_times))
logger.info("CONVERSATION_ONLY: " + str(conversation_only))
logger.info("ENABLE_LIMIT:      " + str(enable_limit))
logger.info("UPLOAD_BY_URL:     " + str(upload_by_url))
logger.info("CHECK_MODEL:       " + str(check_model))
logger.info("SCHEDULED_REFRESH: " + str(scheduled_refresh))
logger.info("RANDOM_TOKEN:      " + str(random_token))
logger.info("OAI_LANGUAGE:      " + str(oai_language))
logger.info("------------------------- Gateway --------------------------")
logger.info("ENABLE_GATEWAY:    " + str(enable_gateway))
logger.info("AUTO_SEED:         " + str(auto_seed))
logger.info("FORCE_NO_HISTORY: " + str(force_no_history))
logger.info("-" * 60)
