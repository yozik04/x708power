export EMAIL="yozik04@gmail.com"
export DEBFULLNAME="Jevgeni Kiski"

tarball:
	python3 setup.py sdist

metadata:
	dh_make -p x708power_1.0.0 -f dist/x708power-1.0.0.tar.gz

deb:
	debuild -b -us -uc

clean:
	debuild -T clean