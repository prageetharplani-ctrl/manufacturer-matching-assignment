PY ?= python3

run:      ## the German aluminium buyer, end to end
	$(PY) -m veridia match --example buyer_de

demo:     ## the two-buyer capacity scenario
	$(PY) -m veridia demo

quality:  ## data-quality report
	$(PY) -m veridia quality

test:     ## full test suite (stdlib unittest; pytest also works)
	$(PY) -m unittest discover -s tests -t . -v

serve:    ## HTTP API on 127.0.0.1:8000
	$(PY) -m veridia serve

.PHONY: run demo quality test serve
