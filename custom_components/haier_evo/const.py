DOMAIN = "haier_evo"
COMMON_LIMIT_CALLS = 5
COMMON_LIMIT_PERIOD = 60
LOGIN_LIMIT_CALLS = 1
LOGIN_LIMIT_PERIOD = 15
LOGIN_LIMIT_MAX = 900
LOGIN_LIMIT_429 = 60
LOGIN_LIMIT_500 = 60
REFRESH_LIMIT_CALLS = 1
REFRESH_LIMIT_PERIOD = 15
REFRESH_LIMIT_MAX = 900
REFRESH_LIMIT_429 = 60
REFRESH_LIMIT_500 = 60
API_HTTP_ROUTE = True
API_TIMEOUT = 15
# HTTP: a part of new TCP connections to iot-platform gets no SYN-ACK, so the
# connect timeout is short and connection errors are retried (reads only for GET).
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 15
API_CONNECT_RETRIES = 3
API_READ_RETRIES = 2
API_PATH = "https://evo.haieronline.ru"
API_LOGIN = "v2/{region}/users/auth/sign-in"
API_TOKEN_REFRESH = "v2/{region}/users/auth/refresh"
API_DEVICES = "v2/{region}/pages/sduiRawPaginated/smartHome?part=1&partitionWeight=6"
API_STATUS = "https://iot-platform.evo.haieronline.ru/mobile-backend-service/api/v1/config/{mac}?type=DETAILED"
API_WS_PATH = "wss://iot-platform.evo.haieronline.ru/gateway-ws-service/ws/"
# WebSocket: TCP connect + TLS + upgrade must finish in WS_CONNECT_TIMEOUT s,
# a ping is sent every WS_PING_INTERVAL s and a missing pong drops the socket.
WS_CONNECT_TIMEOUT = 10
WS_PING_INTERVAL = 15
WS_PING_TIMEOUT = 10
# A command waits up to WS_SEND_WAIT s for the socket to (re)connect and
# WS_ACK_TIMEOUT s for command_response, then it is re-sent once on a new socket.
WS_SEND_WAIT = 20
WS_ACK_TIMEOUT = 6
# How many setups in a row may retry because the cloud returned fewer devices
# than the device registry knows before the integration starts with what it got.
SETUP_INCOMPLETE_RETRIES = 3
