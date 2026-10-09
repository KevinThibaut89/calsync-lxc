"""CalSync — one-way sync from an Outlook/Microsoft 365 calendar to Google Calendar.

Server-side only: it authenticates to Microsoft Graph as a calendar client
(device-code flow, no app registration) and writes into a dedicated Google
calendar. Pure Python standard library, no third-party packages.
"""

__version__ = "0.1.0"
