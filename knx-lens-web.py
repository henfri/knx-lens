from textual_serve.server import Server

server = Server("python -m knx-lens")
server.host="0.0.0.0"
server.serve()