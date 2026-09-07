# Kairos — common tasks.
#
# Everything here is a short shell command; nothing is hidden. Run `make help`
# for the list.

PREFIX     ?= $(HOME)/.local
PYTHON     ?= python3
APP_ID      = org.kairos.Calendar
VERSION     = $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' kairos/__init__.py)
ICON_SIZES  = 16 24 32 48 64 128 256 512

.PHONY: help run test lint install uninstall appimage clean check-deps icons \
        screenshots

help:
	@echo "Kairos $(VERSION)"
	@echo
	@echo "  make run         run the app from this checkout"
	@echo "  make test        run the test suite"
	@echo "  make lint        check for unused imports and undefined names"
	@echo "  make install     install for the current user into $(PREFIX)"
	@echo "  make uninstall   remove it again"
	@echo "  make appimage    build a self-contained AppImage into build/"
	@echo "  make icons SRC=x.png   regenerate the icon set from an image"
	@echo "  make screenshots recapture the ones used in the documentation"
	@echo "  make check-deps  report which dependencies are missing"
	@echo "  make clean       delete build artefacts and __pycache__"

run:
	$(PYTHON) -m kairos

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

lint:
	@$(PYTHON) -m pyflakes kairos tests packaging/*.py \
		|| echo "(install pyflakes for this: pip install pyflakes)"

check-deps:
	@echo "Checking dependencies..."
	@$(PYTHON) -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); \
	  from gi.repository import Gtk, Adw; \
	  print(f"  ok   GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}")' \
	  || echo "  MISSING  python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
	@for module in caldav icalendar recurring_ical_events keyring; do \
	  $(PYTHON) -c "import $$module" 2>/dev/null \
	    && echo "  ok   $$module" \
	    || echo "  MISSING  $$module   (pip install -r requirements.txt)"; \
	done
	@$(PYTHON) -c 'import radicale' 2>/dev/null \
	  && echo "  ok   radicale (CalDAV integration tests will run)" \
	  || echo "  --   radicale not installed; CalDAV integration tests will skip"

install:
	@echo "Installing Kairos $(VERSION) into $(PREFIX)"
	install -d $(PREFIX)/share/kairos
	cp -r kairos $(PREFIX)/share/kairos/
	find $(PREFIX)/share/kairos -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	install -d $(PREFIX)/bin
	sed -e 's|@PREFIX@|$(PREFIX)|g' -e 's|@PYTHON@|$(PYTHON)|g' \
		packaging/kairos.in > $(PREFIX)/bin/kairos
	chmod +x $(PREFIX)/bin/kairos
	install -d $(PREFIX)/share/applications
	install -m644 data/$(APP_ID).desktop $(PREFIX)/share/applications/
	for size in $(ICON_SIZES); do \
	  install -d $(PREFIX)/share/icons/hicolor/$${size}x$${size}/apps; \
	  install -m644 data/icons/hicolor/$${size}x$${size}/apps/$(APP_ID).png \
	    $(PREFIX)/share/icons/hicolor/$${size}x$${size}/apps/; \
	done
	install -d $(PREFIX)/share/metainfo
	install -m644 data/$(APP_ID).metainfo.xml $(PREFIX)/share/metainfo/
	-update-desktop-database $(PREFIX)/share/applications 2>/dev/null
	-gtk-update-icon-cache -f -t $(PREFIX)/share/icons/hicolor 2>/dev/null
	@echo
	@echo "Installed. Run it with: kairos"
	@echo "(If that is not found, add $(PREFIX)/bin to your PATH.)"

uninstall:
	rm -rf $(PREFIX)/share/kairos
	rm -f  $(PREFIX)/bin/kairos
	rm -f  $(PREFIX)/share/applications/$(APP_ID).desktop
	for size in $(ICON_SIZES); do \
	  rm -f $(PREFIX)/share/icons/hicolor/$${size}x$${size}/apps/$(APP_ID).png; \
	done
	rm -f  $(PREFIX)/share/metainfo/$(APP_ID).metainfo.xml
	@echo "Removed. Your calendars and settings in ~/.config/kairos are untouched;"
	@echo "delete that directory too if you want them gone."

icons:
	@test -n "$(SRC)" || { echo "Usage: make icons SRC=path/to/icon.png"; exit 1; }
	./packaging/make-icons.py "$(SRC)"

screenshots:
	./packaging/make-screenshots.py

appimage:
	./packaging/build-appimage.sh

clean:
	rm -rf build dist *.egg-info
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
