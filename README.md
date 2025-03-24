# rocket-exchange
A simple server to integrate status and presence from MS Exchange to Rocket.Chat

What it will do:
- Include custom variables for sync rerun interval, amount of users to sync;
- Run on potatoes, glue and spit;
- Change Rocket.Chat status and description depending on the event in Exchange;
- Use Pexip Event Sinks to relay ad-hoc meetings (not in calendar) happening in on-prem Infinity.

What it won't do:
- Clean your room
- Make world peace happen

Requirements:
- 2 vCPU and 2GB RAM
- Any Linux distro that you're familiar with
- A nice enough network engineer

Platform-specific requirements:
- Pexip Infinity v37
- Rocket.Chat v7.2.5

Installation and use:
1. Clone this repository
2. Read the included readme.txt for further instructions
3. Install requirements.txt
4. Populate .env with required values
5. Run the Python script
