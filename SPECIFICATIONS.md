SPACEPORT BAZAAR
STUDENT FIELD MANUAL / OPENING CHALLENGE
HANDBOOK TO
THE GALAXY

Build a client you can trust. Keep nine planets alive.

WATER
FOOD
COMPONENTS

INSIDE THE HANDBOOK

02 Your assignment
03 The planetary economy
04 Establish a connection
05 Read the galaxy
06 Trade and mutual aid
07 Design your client
08 Results, duplicates, and rejections
09 Prove readiness

AI ENGINEERING / STUDENT GUIDE

PROTOCOL 2.0

01 Your assignment

ATTENTION, FORWARD DEPLOYED ENGINEERS. The Galactic Directorate has reviewed the budget for saving civilization and approved exactly two of you.

You and your teammate have been assigned a planet in the Spaceport Bazaar. Your orders: build the software that observes its condition, controls its trading, and keeps its population supplied. Somewhere below your orbital workstation, an entire world is assuming you know what you are doing. Let us make that assumption defensible.

The opening simulation contains nine planets, each operated by a student pair. All nine teams begin with cooperative intentions. Your classmates are the supply network. Please adjust your diplomatic tone accordingly.

WE ARE SAVING A GALAXY, PEOPLE.

The class wins by completing the run without any of the nine planets ever reaching zero health. All nine must survive. Becoming the wealthiest entry in an extinction report does not constitute victory.

There will also be prizes for prosperous nations: those that accumulate the most overall resources by the end and lose the least health during the simulation. The Directorate supports ambition. It would simply prefer that your trading partners remain alive to admire your success.

Equipment requisition: approved

You have been issued this handbook and one unfamiliar file bundle. Procurement considers this generous. Investigate what you have received, work out how to inspect its contents, and follow the instructions you find. Document your discoveries so your teammate can reproduce the setup without consulting an oracle.

Your first act of planetary stewardship

Build a robust engineering project. Choose any programming language in which both of you can write, explain, and judge clean, expressive code. Establish reproducible setup, tests, meaningful coverage, and a clear design for world data, communication, and decisions. The galaxy has no preferred language. It has strong opinions about running out of food.

AI can and should help with project setup, generated bindings, test scaffolding, and low-level infrastructure. Review and test its work. Your pair owns the data model, architecture, and controlling logic. Both engineers must understand the design and care about its beauty. “The machine wrote it” is not an engineering qualification. If your entire contribution was pressing Accept, the Directorate could have issued the planet two paperweights.

MISSION ORDER: establish a project both engineers can run and explain. Understand your planet. Send a command. Read the response. Build something worthy of the unreasonable number of lives now depending on your software.

02 The planetary economy

Every planet produces one specialty: water, food, or components. Every planet consumes all three. The opening roster has three producers of each resource. You cannot manufacture your way out of every shortage; you must exchange resources with other planets.

What happens on a tick

A tick is one step of simulation time. While the run is RUNNING, the server advances ticks and performs the economy automatically:

Expire offers and advertisements whose deadlines have arrived.
Add each planet's production for this tick to its inventory.
Consume that planet's upkeep from each of the three resources.
Apply health damage for missing upkeep, or recovery if all upkeep was supplied.

Trades can settle between ticks, as soon as the server processes an acceptance. Production, consumption, and health updates are automatic while the simulation is running.

Production changes; reserves matter

Your specialty is fixed for the run, but its output varies over time. Production is credited automatically; there is no harvesting command. self.last_production reports what actually arrived on the most recent tick, not what will arrive next. At tick zero it is zero. Future production schedules and other planets' specialties are not supplied to your client.

The instructor chooses the run's timing, starting stocks, and production surplus. Surplus describes total production relative to total upkeep over the whole run. It is not a guarantee that every planet has enough of every resource at every moment.

For example, with upkeep of one unit of each resource per planet, nine planets need nine water, nine food, and nine components per tick. A 50% production surplus means 50% more of each resource over the full run, excluding starting stocks. Timing and distribution still determine whether anyone goes hungry.

Health is a hard constraint

The standard classroom rules start health at 100, consume one unit of each resource per tick, subtract five health per missing unit, and restore five health on a fully supplied tick, up to 100. Read your actual rules and self.upkeep_per_tick; do not hard-code these numbers.

Example under those rules: start with (2 water, 0 food, 1 component) and produce three water. After upkeep, inventory is (4, 0, 0). One food unit was missing, so health falls by five. Extra water cannot substitute for food.

Zero health is permanent failure for the run. Trading is disabled, open offers involving that planet and its active advertisement are withdrawn, and gifts can no longer rescue it. Passive production and health recovery continue, but neither restores trading nor erases the failure.

03 Establish a connection

Your client talks to the Bazaar server over a WebSocket: a persistent connection on which either side can send messages. Keep receiving while you think, wait, or send commands. Other teams can change the world without waiting for your next request.

Make the destination configuration

Accept the supplied server address and port at runtime, without source edits or recompilation. Prefer a configurable full endpoint such as:

ws://HOST:PORT/ws

with support for:

wss://

when specified. Keep the access token configurable too.

Generate bindings from the message schema, bazaar.proto, for your chosen language and use a compatible Protobuf runtime.

For this starter, select the WebSocket subprotocol bazaar.protobuf.v2 and authenticate with these connection headers:

Authorization: Bearer <your planet token>
Sec-WebSocket-Protocol: bazaar.protobuf.v2

Check that the server selects the requested subprotocol. Choose a WebSocket library that can set the authentication header. The browser's built-in WebSocket API cannot set that header directly. Keep credentials out of source control and routine logs.

One message at a time, in both directions

Send one bazaar.v2.ClientMessage as binary Protobuf bytes per WebSocket message. Decode incoming data messages as bazaar.v2.ServerMessage. Do not add a JSON wrapper, Base64 encoding, or your own length prefix. WebSocket ping, pong, and close controls are handled separately from game messages.

The connection sequence
Connect with the endpoint, token, and subprotocol.
Receive the initial state. Read your planet’s identity, the current rules, and the phase of play.
Send ready with ready: true, following the required message structure in the schema.
Wait for the matching readiness confirmation with ready: true.
Begin new trading actions only when the state also says RUNNING.

Readiness does not start the simulation; the instructor does. Wait during READY and PAUSED. Stop new trading actions at FINISHED or ABORTED.

Coordinate with your teammate: decide how you will run the client, observe incoming messages, and check that the program understands what it receives.

04 Read the galaxy

The schema defines exact field names and message shapes. This handbook explains what the messages mean.

Messages your client sends
Message	Meaning
advertise	Publish the resources you claim to sell or seek.
offer	Propose exact terms to one other planet.
accept	Settle an open offer addressed to your planet.
withdraw	Withdraw your own open offer or active advertisement.
ready	Confirm that this connection has read a snapshot and is ready.
sync	Request a fresh snapshot; it does not advance simulation time.

The first four are gameplay commands. ready and sync are control messages. Use protocol version 2.0 and consult the schema for each message's required fields.

Messages your client receives
Message	Meaning
state	A complete current snapshot of the world you are allowed to see.
result	One command's outcome. Inspect ok and code.
readiness	Acknowledges your readiness declaration for this connection.
protocol_error	A control-level problem with a message. Inspect code.
What is visible

Your self contains inventory, health, failure status, upkeep, specialty, latest production, and cumulative supply and trade counters. The directory contains planet IDs and display names. It does not disclose their inventories, health, specialties, or future output to your client.

Public advertisements reveal what planets claim to sell or need. offers and transactions contain your own proposed and settled exchanges; their terms are private to the two parties. request_results records your commands’ outcomes. Discover potential suppliers through advertisements and experience, not assumptions about planet IDs.

Three different clocks
tick governs production, upkeep, and deadlines.
world_version tracks shared-state revisions; several can occur in one tick.
snapshot_sequence orders snapshots on this connection.

Replace your previous authoritative view with each newer snapshot from the current connection. Do not apply its transactions to inventory again: the reported balance already includes them. A newer snapshot can contain new results even when the world version is unchanged. Keep local estimates and pending decisions separate from server facts.

05 Trade and mutual aid

There is no currency or automatic market matching. An advertisement expresses interest; an offer proposes an exchange; acceptance moves resources. The server authenticates who made a claim, but does not verify advertised availability or need.

A complete exchange

Suppose your planet has (10 water, 4 food, 3 components). At tick 7 you propose giving six water to P02 in exchange for five food, expiring at tick 12. These are the logical contents, not bytes or text to send directly:

offer
  recipient_id: P02
  give:    { water: 6, food: 0, components: 0 }
  receive: { water: 0, food: 5, components: 0 }
  expires_tick: 12

give and receive always use the proposer's perspective. P02 would pay five food and receive six water.

Your successful posting result supplies an object ID. No resources move yet; both parties see the offer in their snapshots.

P02 sends accept with that offer_id. If the offer is still open, both planets remain eligible to trade, and both can pay, the server transfers both bundles together. Otherwise nothing moves. This is atomic settlement.

With no intervening production, upkeep, or trades, your inventory becomes (4, 9, 3). A transaction records the exchange. Read the updated snapshot for the authoritative balance. These quantities illustrate mechanics, not a recommended price or reserve policy.

Signal a need; offer assistance

An advertisement has selling, seeking, and expires_tick. You can publish empty selling and nonempty seeking to request help. There is one active advertisement per planet; a new one replaces the previous one. There is no separate help-request command.

A gift is an ordinary offer with a positive give and an all-zero receive. The recipient must accept it. Aid is voluntary and uses existing inventory. After receiving aid, update or withdraw an outdated advertisement yourself; settlement does not clear it.

Rules your model must express
Whole resources: bundles contain water, food, and components, including zeros. Quantities are nonnegative integers. Give something; do not put the same resource on both sides of an offer.
Exclusive deadlines: expiry at tick 12 means unusable from tick 12 onward. Respect the current TTL limits and the run's end.
No reservation: posting an offer checks your current ability to pay but locks nothing. Several open offers can promise the same stock. Budget for commitments and upcoming upkeep.
Fixed terms: only the recipient accepts; only the creator withdraws. Revise terms by withdrawing and proposing again. An acceptance processed first cannot be undone by a later withdrawal.
06 Design your client

Before building a large decision loop, design the code you want to read at its center. With your teammate, sketch the top-level API for receiving an observation, understanding needs and commitments, and choosing actions. Try it against a few concrete situations before committing to an abstraction.

Give the world a precise vocabulary

Model resource bundles, planet identities, offers, transactions, deadlines, and command outcomes explicitly. Choose idiomatic types and operations for your language. Make it difficult to confuse incoming terms with outgoing terms, a proposed exchange with a completed transaction, or an advertised resource with confirmed stock.

A generated Protobuf class describes the wire. Decide deliberately how it should relate to your domain model. Your trading logic should be able to ask useful questions without repeatedly unpacking transport fields or rebuilding resource arithmetic.

Separate responsibilities
Responsibility	What it should own
Connection and codec	Endpoint, authentication, sockets, encoding, and decoding.
State and command tracking	Current world view and decoded command outcomes.
World model	Resource arithmetic, offer meaning, expiry, commitments, supply estimates.
Decision policy	When to trade, with whom, on what terms, and how to protect reserves.
Execution and evidence	Validate and send chosen actions; record enough to explain their outcomes.

These are boundaries to discuss, not a requirement for five classes, services, or directories. Keep the design as small as the problem permits. It will evolve as you learn how WebSockets behave.

Design from the caller's perspective

A useful conceptual boundary is:

decide(observation, policy_memory)
    -> proposed_actions, next_policy_memory

This is a design prompt, not a supplied API. Choose names and shapes that feel natural in your language. Keep the decision code testable without a live socket. Avoid hiding a network call inside an innocent-looking model query.

Ask together: How do we express “stock available after commitments”? What does an incoming gift look like? How will our API distinguish a proposal from a completed trade? What happens if a newer snapshot arrives while we are deciding?

Own the design

AI can help implement a codec adapter or draft a test fixture. It cannot take responsibility for your strategy, abstractions, or understanding. Both partners should review the controlling code and be able to explain its behavior on an unfamiliar example. Aim for code whose names, types, and control flow make the policy clear without translating it line by line.

07 Results and rejections

A command is a request for the server to act. Even a correctly encoded request can be rejected: the offer may have expired, the resources may no longer be available, or the simulation may not be running.

Success means something specific

A successful advertise means your claim was published. A successful offer means your proposal was created. Neither means a trade happened. A successful accept settles the exchange and creates a transaction. Read each result in the context of the action it answers.

An unsuccessful result is useful information. Your program should recognize it, explain it, and let the decision logic respond appropriately. Printing every response as “command sent” is an ambitious interpretation of reality.

Duplicates deserve attention

Your client may encounter repeated messages. Consider what your program does when it sees the same information twice. Receiving another message does not necessarily mean another transfer occurred. Test duplicates as well as successful commands and rejections; investigate how the protocol represents each case.

Common result codes

These are logical result codes; Protobuf enum names add RESULT_CODE_.

Result code	Meaning
INSUFFICIENT_RESOURCES	A party cannot pay. A failed acceptance moves nothing and leaves the offer open.
EXPIRED / NOT_OPEN	The object has expired or is no longer available for this action.
RATE_LIMITED	Your planet has reached its command quota; consult retry_after_tick.
LIMIT_REACHED	A limit on game objects prevents the action.
RUN_NOT_RUNNING	New trading actions are unavailable in the current phase.
STATION_FAILED	A planet involved in the action has permanently lost trading eligibility.
INVALID_ARGUMENT / NOT_FOUND	Check command values, object references, and which objects you may act on.
REQUEST_ID_CONFLICT	The server reports a conflict in command identity. Consult the protocol.

A protocol_error is a separate control message, not one of these results. Inspect its code to understand why the message could not be processed. Read command and object limits from rules.

A rejected action is a normal possibility in a changing market. Build tests that show your client understands the answer, rather than merely proving it can ask the question.

08 Prove readiness

Your first delivery should give both engineers a foundation they can understand, test, and extend.

Establish a project both partners can use
Version control, documented setup and dependencies, and commands to build, run, test, and measure coverage.
Configurable endpoint and credentials; demonstrate changing the server address or port without editing code.
A short architecture note and examples of your intended decision API.
A receive loop, readiness handshake, and evidence of sending and receiving messages correctly.
Logs linking decisions, input state, commands, results, and subsequent snapshots, without exposing tokens.
Test behavior at the boundaries
Test area	Evidence worth having
Resource model	Correct bundle arithmetic, offer perspective, reserves, and outstanding commitments.
Wire and lifecycle	Required fields and zeros survive encoding; dispatch, readiness, and phase gates work.
State handling	Repeated snapshots do not double-count trades; observations remain consistent.
Trade decisions	Gifts, competing commitments, changed balances, and the exact expiry boundary.
Command outcomes	Successful commands, duplicate messages, and meaningful handling of rejections.
Survival	Low stock, changing production, shortages, and permanent failure despite health recovery.

Use unit tests for models and policies, message fixtures for state handling, and integration tests for sending and receiving. Assert what your client understood and how it responded. A terminal full of messages is not evidence that the program interpreted them correctly.

Measure coverage of handwritten code, including decision branches and failure paths. Review gaps and explain exclusions such as generated bindings. A percentage alone cannot prove correctness.

The first live objective

Start with a policy you can explain: protect upkeep reserves, discover suppliers, and keep resources circulating. Test assumptions with other pairs. Nine well-intentioned programs can still all wait, overcommit, or respond too late.

The opening Bazaar is cooperative. Later, supplies may tighten, trading relationships may weaken, and claims may become less reliable. Build enough clarity and evidence to notice when the world stops matching your assumptions.

Your first duty is to keep your planet supplied. Your shared duty is to keep all nine alive. Your final duty is to bring glory to your planet; the Directorate will accept full credit, having heroically selected you to do all the work.