from textual_serve.server import Server

server = Server("python -m knx_tui_tool")
server.serve()