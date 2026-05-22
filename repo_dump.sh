#!/usr/bin/bash

for file in `find -not -path '*/venv/*' -type f -iname '*.py'`; do
	echo $file
	cat $file
done
