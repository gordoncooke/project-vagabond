# Install the OS on the SD Card

## Prepare the micro SD Card
1. Insert the micro SD card in a read and inster the reader into the computer.
2. On Windows: Open Explorer, go to This PC, and find the drive with the micro SD card in it.
3. Copy any existing data tat you want to save off the SD card to another location. The following process is going to permanetly delete everyting on the card.
CAUTION: Make sure know which drive is the micro SD card! Formatting or imaging the wrong drive could break or delete you system.

If you have a brand new micro SD card you may want to test its capacity to ensure you have not been scammed.  [H2testw](https://h2testw.org/) is one application that can thoroughly test your card. It can take severl hours on larger capacity cards. [MediaTester](https://github.com/dkrahmer/MediaTester) is another application that can also check the caapcity of the card and is faster.

## Install the Imager
1. Download the Raspberry Pi Imager from https://www.raspberrypi.com/software/ (as of 13 OCT 2026 it was imager_2.0.12.exe)
2. Run the imager.
3. Select language: English
4. Agree to licensing terms
5. Default install location (C:\Program Files\Raspberry Pi Ltd\Imager)
6. Click install when Windows security asks
7. Finish and Launch the Raspberry Pi Imager software

## Image the SD Card
1. Select the Raspberry Pi device (Raspberry Pi 3)
2. Chose the operating system. Select the Raspberry Pi OS (Other) then choose Raspberry Pi OS Lite (64-bit). This is based on Debian Trixie with no desktop (as of 13 OCT 2026 it is rel 2026-10-06).
3. Select your storage device. Make sure you select the correct device.
4. Chose a Hostename: enter a hostname you will use. (library)
5. Choose your national capital, time zone, and keyboard layout.
6. Choose your username and password. This will be the user with sudo access to administer the linux OS.
7. Chose your Wi-fi: Enter the SSID and password of your home network.
8. Enable the SSH: turn on ssh so you will be able to administer the device from your computer over an SSH connection. I sued password authentication. You can configure public key authentication later, or now if you prefer, and know how.
9. Choose Raspberry Pi Connect: I left this off. It is not needed for this device to work.
10. Confirm that the device matches teh one you have (Rapsberry Pi 3), you are installing Rapsberry Pi OS Lite (64-bit), and the correct storage device is selected. 
CAUTION: Make sure know which drive is the micro SD card! Formatting or imaging the wrong drive could break or delete you system.
11. Click Write and then confirm to proceed with imaging the SD card.
12. Wait for it to finins and click Finish.

# Connect to your new system

## Connect to the new system
1. Install the SD card in the Raspberry Pi.
2. Power on the system and wait a minute or two to allow it time to boot and connect to your home wifi.
3. On Windows: Open the command line or Powershell.
4. Enter `ssh your_username@your_hostname` but substitute the username and hostname you imaged the sd card with.
5. The first time you connect, Windows will ask you to confirm you want to connect becasue this is a new, unknown, system. It will store the ID of teh device and will not ask again in the future.
6. Type `yes` and click enter.
7. You should then be connected and working on the linux system on the Raspberry Pi. The propmt will look like `user@library:~ $`

##
