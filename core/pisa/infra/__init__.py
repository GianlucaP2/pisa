"""Infrastructure the brain and the services share: provider keys at rest, the spend ledger and its caps,
the hard limit over every paid service, and the exchange rate.

It sits below the brain and the services (both import it) and above the ports and the domain; it never
imports the brain or the services.
"""
