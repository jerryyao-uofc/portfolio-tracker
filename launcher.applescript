on run
	set appPath to POSIX path of (path to me)
	set root to do shell script "dirname " & quoted form of appPath
	do shell script quoted form of (root & "/start-tracker.sh")
end run
