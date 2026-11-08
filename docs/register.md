# Register: what ran and what did not

| Ran here | Next to try |
|---|---|
| Postgres 17 and Meilisearch 1.12.8 in Docker on Windows 11 | a supplier's own feed |
| an SFTP server (paramiko) running inside the test process | a real SFTP server, key based login |
| local fake REST and SOAP servers (zeep against a WSDL I wrote) | a real REST API or SOAP service, WS-Security, cursor pagination |
| sample X12 documents, read back by my own reader | any trading partner, VAN or EDI translator |
| the data generator's truth file as the oracle for matching | real part numbers, real ACES or VCdb reference data |
| 200,000 rows on one machine | millions of rows, several workers at once (only the advisory lock is tested) |
| the CI workflow file | a run of that workflow on GitHub |
| a local stand-in shop site (login with a CSRF token, expiring session, rate limit, challenge page) and a local forwarding proxy, for the scraper | any real site, a real proxy service, a real bot challenge or web firewall |
| local Docker Compose stack | Linux and macOS |
