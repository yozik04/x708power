from setuptools import find_packages, setup

setup(
    name="x708power",
    version="1.0.0",
    packages=find_packages(),
    url="https://github.com/yozik04/x708power",
    license="MIT",
    author="Jevgeni Kiski",
    author_email="yozik04@gmail.com",
    description="x708 Automatic Safe Shutdown",
    install_requires=["RPi.GPIO~=0.7.0", "smbus~=1.1.0"],
    scripts=["bin/x708daemon"],
)
