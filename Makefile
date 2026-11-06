.PHONY : pc
pc:
	python scripts/versions.py --path=recipes --commit --doc PACKAGES.md --index PACKAGES.json
