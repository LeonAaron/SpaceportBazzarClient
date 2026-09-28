# Task Summaries

Plain-language summaries of the work completed for Tasks 14, 7, and 19.

## Task 14 — Following an Offer Through the System

This task asked us to trace how information flows through our code — specifically, what happens to a single trade offer from the moment it arrives until it's completed.

1. **A complete, documented trail.** We wrote up the entire journey of an incoming offer in our architecture document, following it through eight distinct steps, from the moment it's received to the final record of the trade. Each step points to the exact place in our code where it happens, so anyone can verify the trail themselves. This turns an abstract description of "how the system works" into a concrete, checkable map.

2. **Proof it works on real data, not just on paper.** We built a tool that can take a past game session and, given a single offer's ID, reconstruct exactly what our program knew, decided, sent, and received back for that one offer. This means our documented trail isn't just a theoretical description — it can be demonstrated and verified against real runs. It gives us, and anyone reviewing our work, concrete evidence rather than just a written claim.

3. **Separating "thinking" from "doing."** We reorganized the code so the part that decides whether to accept a trade is completely separate from the parts that handle communication and logging. This keeps the decision-making logic clean, isolated, and easy to trace on its own. We also added a test that enforces this separation, so future changes can't accidentally blur the two together.

## Task 7 — A Clear Record of Every Game, Automatically Summarized

This task was assigned to us as a natural extension of Task 14: once we could trace one offer, we built the tools to record and summarize everything that happens in an entire game.

1. **A simple, consistent record-keeping format.** We designed and documented a log format where every important event — starting a game, connecting to a server, making a decision, sending a command, ending a game — is written as one line of easy-to-read text. Every entry is linked to the ones that caused it, so a trade can always be traced back to the decision and offer that led to it. This makes the log both easy to read and easy to search or analyze later.

2. **Turning raw logs into useful reports.** We built a script that reads a log and automatically produces either a short text summary (trades made, offers rejected, shortages, how responsive the system was) or a full interactive report viewable in a web browser, complete with charts, a searchable timeline of decisions, and connection reliability stats. This means anyone, technical or not, can quickly understand what happened in a game without reading the raw log. It also lets us spot problems or interesting patterns without hunting through data by hand.

3. **Built to work anywhere, for anyone.** The summary tool only uses basic, built-in Python features, with no extra software installation required. This means any team, even one without access to our full project, can run it and read the results. It keeps the tool simple, portable, and easy to share.

## Task 19 — A Standalone Game Server

Although this task was originally assigned to other teams, we built our own version of it because it let us fully demonstrate the tracing and reporting work from Tasks 7 and 14 on real, multi-round games with multiple players.

1. **A real server other teams can connect to.** We built a server that speaks the exact same communication protocol as the official game client, meaning any team's client, including ones we didn't write, can connect and play against it. This makes our server broadly compatible and useful beyond just our own project. It also proves our understanding of the communication protocol is correct, not just assumed.

2. **Simple, flexible player sign-in.** Following the course's suggested approach, players can join either with a personal access token or, in an open mode, simply by connecting in order until all seats are filled. This keeps the setup lightweight and easy to use for testing or demonstrations. It avoids unnecessary complexity while still supporting controlled access when needed.

3. **One command to run a whole game.** We built a script that starts the server and all the player programs (from three to nine of them) automatically, runs a complete game from start to finish, and then produces a scored report along with individual dashboards for each player. This turns what would otherwise be a manual, multi-step process into a single, repeatable command, making it much easier to run demonstrations or repeated tests.

4. **Verified against real, worked-out examples.** Every rule the server enforces — such as how goods are produced, how trades settle, when offers expire, and limits on how fast players can act — was tested against real examples we worked out by hand, using an actual live connection rather than a simulation. This gives strong confidence that the server behaves correctly under real conditions, not just in theory, and means any bugs were caught early, before other teams would rely on the server.
