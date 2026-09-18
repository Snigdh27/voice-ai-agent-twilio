Workato Genie Voice Integration

This project connects Twilio Voice with OpenAI GPT-Live and
Workato Genie.

Twilio sends the live phone-call audio to the application through a
WebSocket media stream. The application connects that stream to
GPT-Live, delegates business requests to Workato Genie, and sends the
generated voice response back to Twilio.

Prerequisites

Before running the application, make sure you have:

Python 3 installed

A Twilio account and a voice-enabled Twilio phone number

An OpenAI API key

Workato Genie API credentials

A public HTTPS/WSS URL that forwards traffic to the local
application

The required ivr_upward_arp.ulaw audio file in the project
directory

Project Structure

A typical local project layout is:

project/
├── main.py
├── genie.py
├── .env
└── ivr_upward_arp.ulaw

main.py runs the FastAPI application and handles the Twilio/OpenAI
live voice connection.

genie.py handles communication with Workato Genie, including
conversation creation, sending messages, and waiting for Genie
responses.

Install Dependencies

Create and activate a virtual environment if desired, then install the
Python dependencies used by the application:

pip install fastapi uvicorn websockets python-dotenv requests

Environment Configuration

Create a .env file in the same directory as main.py.

OPENAI_API_KEY=<your-openai-api-key>

GENIE_API_BASE_URL=<your-genie-api-base-url>
GENIE_API_KEY=<your-genie-api-key>
GENIE_ID=<your-genie-id>

# Fallback IDP user ID when one is not supplied dynamically by Twilio
GENIE_IDP_USER_ID=<your-idp-user-id>

# Public hostname only — do not include https:// or wss://
PUBLIC_HOST=<your-public-hostname>

# Optional; defaults to 8000
PORT=8000

OPENAI_API_KEY and PUBLIC_HOST are required by the voice
application. The Genie integration requires GENIE_API_BASE_URL,
GENIE_API_KEY, and GENIE_ID. GENIE_IDP_USER_ID can be used as a
fallback when an IDP user ID is not supplied dynamically for the call.

Example

If your hosted URL is:

https://abc123.example-tunnel.app

set:

PUBLIC_HOST=abc123.example-tunnel.app

The application automatically constructs the WebSocket endpoint as:

wss://abc123.example-tunnel.app/media-stream

Run the Application

Start the application with:

python main.py

By default, the FastAPI server listens on:

0.0.0.0:8000

If PORT is configured in .env, that port is used instead.

Expose the Local Server

Twilio must be able to reach the application over the public internet.
During local testing, expose the application using your preferred HTTPS
tunneling or hosting service.

For example, if the application is running on port 8000, a tunnel can
forward a public HTTPS URL to:

localhost:8000

After starting or restarting a temporary tunnel, copy its current public
hostname and update:

PUBLIC_HOST=<new-public-hostname>

Then restart the application so it loads the new value:

python main.py

If your tunnel generates a new URL each time it starts, both the
.env PUBLIC_HOST value and the Twilio webhook must be updated.

Configure the Hosted URL in Twilio

For testing, Twilio must point incoming calls to this application's
/twiml endpoint.

In the Twilio Console:

Open Phone Numbers and select the phone number being used for
testing.

Go to the number's Voice configuration.

Find the setting for handling an incoming call.

Configure it to use a Webhook.

Set the webhook URL to:

https://<PUBLIC_HOST>/twiml

Use the HTTP method supported by the application, normally POST.

Save the Twilio configuration.

For example:

https://abc123.example-tunnel.app/twiml

The /twiml endpoint returns TwiML that instructs Twilio to open the
following media stream:

wss://<PUBLIC_HOST>/media-stream

Dynamic Workato IDP User ID

The /twiml endpoint optionally accepts a Workato IDP user ID as a
query parameter:

https://<PUBLIC_HOST>/twiml?workato_x_idp_user_id=<IDP_USER_ID>

When provided, this value is passed through Twilio's custom stream
parameters and used for the Workato Genie request.

If it is not provided, the application falls back to:

GENIE_IDP_USER_ID=<your-idp-user-id>

Testing Flow

For each local test session:

Start or confirm your public tunnel/host is running.

Copy the current public hostname.

Update PUBLIC_HOST in .env.

Update the Twilio incoming-call webhook to
https://<PUBLIC_HOST>/twiml.

Run:

python main.py

Call the configured Twilio phone number.

Watch the terminal logs for the Twilio stream, GPT-Live session, and
Workato Genie requests/responses.

A successful startup/call should show logs corresponding to events such
as:

TWILIO CALL SETUP
Twilio media stream connected
TWILIO STREAM STARTED
GPT-LIVE SESSION READY

When a business request is delegated, the application also logs the
Genie request and its final response.

Request Flow

Caller
  |
  v
Twilio Phone Number
  |
  |  POST /twiml
  v
FastAPI Application
  |
  |  wss://<PUBLIC_HOST>/media-stream
  v
Twilio Media Stream <----> GPT-Live
                              |
                              | business request delegation
                              v
                         Workato Genie
                              |
                              v
                         Genie response
                              |
                              v
GPT-Live -> Twilio -> Caller

Common Issues

OPENAI_API_KEY is required

Add OPENAI_API_KEY to .env and restart python main.py.

PUBLIC_HOST is required

Set PUBLIC_HOST to the public hostname used for the current test
session. Do not include https:// or wss://.

Missing Genie environment variables

Make sure these values are configured:

GENIE_API_BASE_URL=...
GENIE_API_KEY=...
GENIE_ID=...

No Workato IDP user ID is available

Either provide the ID dynamically in the Twilio /twiml URL:

https://<PUBLIC_HOST>/twiml?workato_x_idp_user_id=<IDP_USER_ID>

or configure:

GENIE_IDP_USER_ID=<IDP_USER_ID>

Music filler not found

Make sure this file exists beside main.py:

ivr_upward_arp.ulaw

Twilio call does not reach the application

Check that:

python main.py is running.

The public tunnel/host forwards to the configured application port.

PUBLIC_HOST contains the current hostname.

The Twilio webhook contains the current public HTTPS URL followed by
/twiml.

The Twilio configuration has been saved.

The public URL supports WebSocket connections.

Important Testing Note

Whenever the temporary hosted/tunnel URL changes, update both
locations before testing again:

.env
PUBLIC_HOST=<new-hostname>

and:

Twilio incoming-call webhook
https://<new-hostname>/twiml

Restart python main.py after changing .env.
