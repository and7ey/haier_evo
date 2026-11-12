from datetime import timedelta

DOMAIN = "haier_evo"
CONF_REGION = "region"
# tokens of the config flow's sign-in, handed over to the entry once (moved to .storage)
CONF_TOKENS = "tokens"
# entry option: control of washers and dryers (beta, 1.7), off by default
CONF_LAUNDRY_CONTROL = "laundry_control"
REGIONS = ["ru", "kz", "by"]

# Login throttling: at most one login per LOGIN_LIMIT_PERIOD seconds,
# after HTTP 429 wait LOGIN_LIMIT_429 seconds, doubling up to LOGIN_LIMIT_MAX.
LOGIN_LIMIT_PERIOD = 15
LOGIN_LIMIT_429 = 60
LOGIN_LIMIT_MAX = 900
# Tokens: a token issued less than TOKEN_FRESH s ago and refused anyway is
# tried once more as is after TOKEN_RETRY_DELAY s, without renewing it: the IoT
# backend learns new tokens with a delay (as in 1.7).
TOKEN_FRESH = 30
TOKEN_RETRY_DELAY = 3

# HTTP, as in 1.7: a part of new TCP connections to iot-platform gets no
# SYN-ACK, so the connect timeout is short and a connection that was not
# established is tried API_CONNECT_RETRIES times more (the request never left:
# safe for the sign-in too). A dropped or timed out answer is tried
# API_READ_RETRIES times more, for GET only. The pauses grow from
# API_RETRY_BACKOFF s, doubling.
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 15
API_CONNECT_RETRIES = 3
API_READ_RETRIES = 2
API_RETRY_BACKOFF = 0.5
API_PATH = "https://evo.haieronline.ru"
API_LOGIN = "v2/{region}/users/auth/sign-in"
API_TOKEN_REFRESH = "v2/{region}/users/auth/refresh"
API_DEVICES = "v2/{region}/pages/sduiRawPaginated/smartHome?part=1&partitionWeight=6"
API_STATUS = "https://iot-platform.evo.haieronline.ru/mobile-backend-service/api/v1/config/{mac}?type=DETAILED"
API_WS_PATH = "wss://iot-platform.evo.haieronline.ru/gateway-ws-service/ws/"

# WebSocket: ping every WS_HEARTBEAT s, a missing pong closes the socket.
WS_HEARTBEAT = 30
# TCP + TLS + upgrade must finish in this time: a stuck handshake used to hold
# the connection (and every command waiting for it) for up to aiohttp's 300 s.
WS_CONNECT_TIMEOUT = 10
# How long a command waits for the socket to come back.
WS_SEND_TIMEOUT = 15
# Reconnect backoff, reset after a connection that lived WS_STABLE_AFTER s.
WS_BACKOFF_MIN = 2
WS_BACKOFF_MAX = 60
WS_STABLE_AFTER = 60
# Entities go unavailable after this long without the cloud.
WS_UNAVAILABLE_AFTER = 180
# After an outage longer than this, re-read device statuses over REST.
WS_RESYNC_AFTER = 30
# Wait this long for command_response before re-sending a command once.
WS_ACK_TIMEOUT = 10
# A service call waits this long for command_response, so a refusal of the
# cloud (non-zero errNo for values the unit does not have) reaches the UI as
# an error. No answer by then: the call returns, the re-send and the read-back
# go on in the background.
WS_COMMAND_WAIT = 5
# Safety net against a session that stays open but goes silent.
REST_REFRESH_INTERVAL = timedelta(minutes=15)
# The device list and descriptions are compared with the cloud this often: a
# unit added in the app, or one the cloud now describes differently (new
# firmware, attributes), reloads the entry (1.6).
DEVICE_LIST_REFRESH_INTERVAL = timedelta(hours=6)
# A session the cloud has sent nothing over for this long since it opened is
# accepted but not served (the cloud pushes statuses within seconds of an
# opening): it is replaced. The timeout doubles with every silent session in a
# row (up to 8x), the WS_SILENT_SESSIONS_FRESH_TOKEN-th one goes with a renewed token.
WS_SILENT_SESSION_TIMEOUT = 60
WS_SILENT_SESSIONS_FRESH_TOKEN = 2
# The gateway may accept the upgrade and then close with this code
# ("Auth token not valid!"): the token is refused.
WS_CLOSE_TOKEN_REFUSED = 1007

# Setups in a row that may retry because the cloud listed fewer devices than
# the device registry knows, before the entry starts with what it got.
SETUP_INCOMPLETE_RETRIES = 3

# Device descriptions of the last good start live in
# .storage/haier_evo.<entry_id>.devices. When the cloud is not reachable at
# start, the entry is built from them; the cloud is then polled (from
# CACHE_RETRY_FIRST s, doubling up to CACHE_RETRY_MAX s) and the entry reloaded
# from it once it answers. Statuses are written back CACHE_SAVE_DELAY s after
# a refresh.
CACHE_VERSION = 1
CACHE_RETRY_FIRST = 5
CACHE_RETRY_MAX = 300
CACHE_SAVE_DELAY = 60

LAUNDRY_REFRESH_INTERVAL = timedelta(seconds=30)
LAUNDRY_PUSH_MAX_AGE = 180
