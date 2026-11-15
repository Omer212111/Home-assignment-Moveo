.PHONY: setup ingest ingest-weather ingest-declarations db-status test

setup:                 ## install dependencies
	uv sync

ingest:                ## download everything (cached responses are reused), rebuild snapshot
	uv run python -m app.ingest

ingest-weather:        ## weather only, e.g. make ingest-weather HUBS=denver,miami
	uv run python -m app.ingest --source weather $(if $(HUBS),--hubs $(HUBS),)

ingest-declarations:   ## FEMA declarations only
	uv run python -m app.ingest --source declarations

db-status:             ## what is in the database
	uv run python -c "from app import db, ingest; db.restore_snapshot(); ingest.print_status()"

test:
	uv run pytest -q
