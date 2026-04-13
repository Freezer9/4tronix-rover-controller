# 4tronix M.A.R.S. Rover installation

This installation guide explains how to install the basic software for your 4tronix M.A.R.S. Rover on a Raspberry Pi Zero so you can then manually control it from a PC/laptop. Future additions are described at the end.

The ultimate aim is to develop an autonomous (i.e. without human interference) search and rescue robot using the 4tronix M.A.R.S. Rover platform.

The base software is based on the test software provided by 4tronix but then integrated into one total library, called from one separate control program. One of the main additions is the Ackermann steering geometry so that the wheels turn to the correct position to ensure minimum slippage (Wikipedia for more info). Also the mast position and LED color and behaviour is linked to the driving behaviour.

This guide assumes basic knowledge about installing and configuring a Raspberry Pi. For more info visit raspberrypi.com and go to the documentation section.

# Installation steps:

1. Assemble the 4tronix M.A.R.S Rover as per the instructions on the 4tronix website.
2. Install the Raspberry Pi operating system for the P Zero W with the Raspberry Pi Imager (Legacy Edition), with SSH enabled, with your WIFI network configured, with your own choice of hostname, user and password.
3. Once the image installation is complete, insert the SD card in your Raspberry Pi and start it up. Find the IP address, start PuTTY, open the IP address and login into the system en then run “sudo apt update” and “sudo apt upgrade”.
4. Run “sudo raspi-config” and enable SPI and I2C in the interface options, followed by a reboot.
5. Copy the following files from github and then, use "scp" to copy them to your Raspberry Pi:

-     roverlib.py
-     app.py
-     fullServoCalibrate.py
-     requirements.txt

6. Switch to root and create/activate a virtual environment:

-     "sudo su"
-     "python -m venv env"
-     "source env/bin/activate"

7. Install project dependencies listed in requirements.txt:

-     "pip install -r requirements.txt"

8. Run "python3 fullServoCalibrate.py" to perform the calibration of the four corner servos. Follow the on-screen instructions to select each servo in turn and move each one into the 3 positions (right 90 degrees,middle,left 90 degrees). Use the arrow keys to make the positions as accurate as possible and press s when ready.
9. After the calibration you can attach the wheels to the motors.
10. Run "python3 app.py" to start the rover control web app.

# Have fun !!!
