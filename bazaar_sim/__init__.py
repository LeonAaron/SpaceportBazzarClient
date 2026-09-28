"""Our own Bazaar economy: the rule engine, a protocol server, and benchmarks.

`bazaar_client` plays the game; this package hosts it. They share only the
domain types and the protobuf mappers, so the server speaks exactly the wire
format the client is tested against.
"""
