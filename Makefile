# Makefile for Sphinx documentation
#

.PHONY: clean html linkcheck

help:
	@echo "Please use \`make <target>' where <target> is one of"
	@echo "  html       to make standalone HTML files"
	@echo "  linkcheck  to check external links"

clean:
	cd ko; make clean; cd ..
	cd en; make clean; cd ..

html:
	cd ko; make html; cd ..
	cd en; make html; cd ..

linkcheck:
	cd ko; make linkcheck; cd ..
	cd en; make linkcheck; cd ..
