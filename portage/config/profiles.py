# Copyright: 2005 Gentoo Foundation
# License: GPL2
# $Id: profiles.py 2272 2005-11-10 00:19:01Z ferringb $

class base(object):
	pass

	def get_data(self, bashrc):
		raise NotImplementedError
	
	get_path = get_data

class ProfileException(Exception):
	def __init__(self, err):	self.err = err
	def __str__(self): return str(self.err)
