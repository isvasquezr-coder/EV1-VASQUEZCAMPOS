#!/bin/bash

mkdir tempdir
mkdir tempdir/templates
mkdir tempdir/static

cp sample_app.py tempdir/.
cp -r templates/* tempdir/templates/.
cp -r static/* tempdir/static/.

echo "FROM python" >> tempdir/DockerFile
echo "RUN pip install flask" >> tempdir/DockerFile
echo "COPY ./static /home/myapp/static/" >> tempdir/DockerFile
echo "COPY ./templates /home/myapp/templates/" >> tempdir/DockerFile
echo "COPY sample_app.py /home/myapp/" >> tempdir/DockerFile
echo "EXPOSE 6789" >> tempdir/DockerFile
echo "CMD python3 /home/myapp/sample_app.py" >> tempdir/DockerFile

cd tempdir

docker build -t sampleapp .

docker run -t -d -p 6789:6789 --name samplerunning sampleapp

docker ps -a
