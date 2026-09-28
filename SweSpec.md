SPACEPORT BAZAAR                                      ENGINEERING SELF-ASSESSMENT

How sound is our client?

Assess the current codebase together. Back each rating with evidence.

Engineers: Leon Aaron, Luke                         Date: 9/28/26

Revision / configuration:
Trade maximum of resource you produce to stabilize reserves

0 NOT YET
Absent or not yet understood.

1 IMPLEMENTED
Present, but not yet demonstrated.

2 DEMONSTRATED
Can show repeatable evidence.

Circle or select one rating per row. Add subtotals and the overall total manually.


1  Launch and operate
   Operational readiness

ASSESSMENT QUESTION

01  Can another pair follow our documentation to install, configure, authenticate, and
    run our client without editing source code?

02  Can we distinguish "process running," "connected," "authenticated," "state
    synchronized," and "actively participating"? Can we recognize stale state?

03  If we cannot connect, does the diagnostic output help us distinguish configuration,
    authentication, protocol, network, and application failures?

Evidence:
Documentation in README.md / logs when connected to server

Section subtotal  5 / 6


2  Observe, explain, and review
   Observability, logging, reporting

ASSESSMENT QUESTION

04  Can we see our reserves, current epoch, pending actions, open offers, and recent
    completed trades in an interpretable view?

05  For a particular offer, can we reconstruct what our client knew, what it decided, why
    it accepted or passed, what it sent, and what the server confirmed?

06  Do structured logs preserve that history after a crash or simulation ends, with
    identifiers that connect related events?

07  Can we generate a useful run summary - resource histories, completed trades,
    rejected requests, disconnected periods, and shortages - without manually reading
    thousands of lines?

Evidence:

Section subtotal  5 / 8


3  Measure responsiveness
   Performance engineering

ASSESSMENT QUESTION

08  Do we separately measure time waiting to process an event, time making a
    decision, and time awaiting the server's response?

09  Do we record decisions to pass or take no action, as well as actions we send? Can
    we distinguish intentional waiting from a stalled client?

10  Can we report typical and slow responses, sample counts, and missed deadlines?
    Does the client continue receiving updates while a strategy is busy?

Evidence:

Section subtotal  1 / 6


4  Understand and change the design
   Architecture, API design, composability

ASSESSMENT QUESTION

11  Can we choose which strategy to run through a configuration setting or
    command-line argument, without changing source code?

12  Can we give our strategy an example planet state and incoming offer, then
    automatically check its decision without connecting to a server or starting the full
    application?

13  Can we change how messages are sent or how activity is logged without rewriting
    our trading strategy?

14  Can each partner explain how an incoming offer moves through the code, where the
    decision happens, and how the response is sent?

Evidence:

Section subtotal  7 / 8

Evidence may include a test, log, report, command, screenshot, or code reference. Both partners should be able to explain it.


3  Test meaningful behavior
   Unit testing and coverage

ASSESSMENT QUESTION

15  Do tests assert expected decisions and state transitions, including boundary cases
    and failure paths?

16  What do line and branch coverage reveal about untested code? Can we explain the
    remaining gaps, excluding generated code from our own-code assessment?

17  Would our tests fail if we deliberately introduced a plausible bug? Have we turned
    failures from previous simulations into regression tests?

Evidence:

Section subtotal  6 / 6


6  Test a functioning trading system
   Integration, simulation, interoperability

ASSESSMENT QUESTION

18  Can we run two clients against our own test server, complete an exchange, and
    verify the resulting inventories?

19  Can we run a local simulation with multiple copies of our strategy, varying the
    number of planets while keeping resource production and consumption balanced?

20  Does our test server implement the documented rules we depend on? Can we
    demonstrate this with small, hand-checked examples and explain what it does not
    simulate?

21  Can our client exchange messages and complete trades with a client developed by
    another pair, using a shared test environment?

Evidence:

Use the provided protocol test server for compatibility checks and your own simulator
for trading behavior. Document the simulator's assumptions and omissions.

Section subtotal  5 / 8


7  Demonstrate improvement
   Benchmarking, reproducibility, continuous verification

ASSESSMENT QUESTION

22  Have we defined success before comparing strategies, including collective survival
    and shortages rather than only our own final stockpile?

23  Have we compared against a simple baseline across multiple matched scenarios,
    assignments, and seeds, reporting failed runs as well as successful ones?

24  Can we reproduce an experiment using its code revision, configuration, scenario,
    and recorded events? Where scheduling introduces nondeterminism, have we
    captured enough evidence to investigate it?

25  Does one command run our checks, and does CI run them on changes? Can we
    identify the exact tested revision we are bringing to class?

Evidence:

Section subtotal  5 / 8


Overall assessment

Add the seven section subtotals. Maximum: 50 points across 25 questions.

Section maximums  1: /6   2: /8   3: /6   4: /8   5: /6   6: /8   7: /8

TOTAL  35 / 50


Our next two improvements

Choose concrete changes. Name the question number and the evidence you will produce to demonstrate progress.

1. Question(s): 2

   Change and evidence:
   Track all action taken during simulation to improve.


2. Question(s): 3

   Change and evidence:
   Understand issue of why errors were taken to refine logs.