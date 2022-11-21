Home Assistant |Chat Status|
=================================================================================


### Installing Python 3.9


```
sudo apt-get update
sudo apt-get upgrade

sudo apt-get install xz-utils
sudo apt-get install build-essential
sudo apt-get install libssl-dev
sudo apt-get install libsqlite3-dev
sudo apt-get install zlib1g-dev
sudo apt-get install libffi-dev

mkdir ~/python_src
cd python_src
wget https://www.python.org/ftp/python/3.9.7/Python-3.9.7.tgz
tar xzvf Python-3.9.7.tgz
cd Python-3.9.7/
./configure --enable-shared --prefix=/home/$USER/Python-3.9.7 --with-ensurepip=install --enable-optimizations
make
make install
```

Edit ~/.bashrc and insert the following
```
vim ~/.bashrc
```
Paste the following:
```
export PATH=/home/$USER/Python-3.9.7/bin:$PATH
export LD_LIBRARY_PATH=/home/$USER/Python-3.9.7/lib
export LD_RUN_PATH=/home/$USER/Python-3.9.7/lib
```

Log out and login, now you should see python3 with new date
```
python3
```
You should see:
```
Python 3.9.7 (default, Dec 10 2019, 19:33:46) 
[GCC 7.4.0] on linux
Type "help", "copyright", "credits" or "license" for more information.
>>> 
```


