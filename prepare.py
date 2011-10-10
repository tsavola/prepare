#!/usr/bin/env python3
#
# Copyright (c) 2011 Timo Savola
# Reverse topological sort function is Copyright (c) 2004, 2005 Nokia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import ast
import optparse
import os
import re
import sys

class Project:

	def __init__(self, outputdir):
		self.outputdir = outputdir
		self.units = []

	def add(self, sourcename):
		targetname = os.path.join(self.outputdir, sourcename[:-1])
		self.units.append(Unit(sourcename, targetname))

	def process(self):
		for unit in self.units:
			unit.parse()

		symbolmap = {}

		for unit in self.units:
			for symbol in unit.declsyms:
				assert symbol.name not in symbolmap
				symbolmap[symbol.name] = symbol

		datamap = {}

		for unit in self.units:
			unit.analyze_references(symbolmap, datamap)

		depends = set()

		for unit in self.units:
			for name in unit.refnames:
				symbol = symbolmap[name]
				depends.add((unit, symbol.unit))

		for data in datamap.values():
			for prod in data.producers:
				for cons in data.consumers:
					depends.add((cons, prod))

		globaldict = {}

		for unit in reverse_topological_sort(self.units, depends):
			unit.evaluate(globaldict)

class Unit:

	def __init__(self, sourcename, targetname):
		self.sourcename = sourcename
		self.targetname = targetname

	def parse(self):
		self.blocks = []
		self.declsyms = set()
		self.refnames = set()

		code_block = False
		code_indent = None
		code_source = ""

		with open(self.sourcename) as sourcefile:
			for text in sourcefile:
				while text:
					if code_block:
						i = text.find("}}}")
						if i >= 0:
							code_block = False
							if text[:i].strip():
								code_source += text[:i]
							self.blocks.append(CodeBlock(code_source, code_indent, self))
							code_indent = None
							code_source = ""
							text = text[i+3:]
						else:
							if text.strip():
								code_source += text
							text = ""
					else:
						i = text.find("{{{")
						if i >= 0:
							code_block = True
							self.blocks.append(TextBlock(text[:i]))
							code_indent = re.sub(r"[^\t]", " ", text[:i])
							text = code_indent + "   " + text[i+3:]
							if not text.strip():
								text = ""
							text = "if True:\n" + text
						else:
							self.blocks.append(TextBlock(text))
							text = ""

		self.refnames -= set(s.name for s in self.declsyms)
		self.refnames -= set(dir(__builtins__))

	def analyze_references(self, symbolmap, datamap):
		def get_data(unit):
			data = datamap.get(unit)
			if data is None:
				data = Data()
				datamap[unit] = data

			return data

		for name in self.refnames:
			symbol = symbolmap[name]

			if symbol.producer:
				get_data(symbol.unit).producers.add(self)

			if symbol.consumer:
				get_data(symbol.unit).consumers.add(self)

	def evaluate(self, globaldict):
		dirname = os.path.dirname(self.targetname)
		if dirname and not os.path.exists(dirname):
			os.makedirs(dirname)

		tempname = self.targetname + ".tmp"
		done = False

		try:
			with open(tempname, "w") as outputfile:
				for block in self.blocks:
					block.evaluate(globaldict, outputfile)

			done = True
		finally:
			if done:
				self.deploy(tempname)
			else:
				os.remove(tempname)

	def deploy(self, tempname):
		changed = True

		if os.path.exists(self.targetname):
			with open(tempname) as file:
				newdata = file.read()

			with open(self.targetname) as file:
				olddata = file.read()

			changed = (newdata != olddata)

		if changed:
			print("  Update   ", self.targetname)
			os.rename(tempname, self.targetname)
		else:
			os.remove(tempname)

class TextBlock:

	def __init__(self, text):
		self.text = text

	def evaluate(self, globaldict, outputfile):
		print(self.text, end="", file=outputfile)

class CodeBlock:

	source_transformations = [(re.compile(pat, re.MULTILINE), sub) for pat, sub in [
		(r"^(\s*)for\s+([^\s:]+)\s+in\s+([^:]+)\s+if\s+([^:]+):",
		 r"\1for \2 in (\2 for \2 in \3 if \4):"),

		(r"(^|\W)echo\s*\((.*)\)(\s*)$",
		 r"\1_echo(locals(), \2)\3"),
	]]

	def __init__(self, source, indent, unit):
		for pat, sub in self.source_transformations:
			source = pat.sub(sub, source)

		self.ast = ast.parse(source, unit.sourcename)
		self.indent = indent
		self.unit = unit
		self.declnames = set()

		self.analyze_symbols(self.ast)

	def analyze_symbols(self, node, toplevel=True):
		if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
			if toplevel and node.name[0].isupper():
				producer = False
				consumer = False

				for decor in node.decorator_list:
					if isinstance(decor, ast.Name):
						if decor.id == "producer":
							producer = True

						if decor.id == "consumer":
							consumer = True

				self.unit.declsyms.add(Symbol(node.name, self.unit, producer, consumer))
				self.declnames.add(node.name)

			toplevel = False

		if isinstance(node, ast.Name) and node.id[0].isupper():
			if isinstance(node.ctx, ast.Store):
				self.unit.declsyms.add(Symbol(node.id, self.unit))
				self.declnames.add(node.id)

			if isinstance(node.ctx, ast.Load):
				self.unit.refnames.add(node.id)

		for child in ast.iter_child_nodes(node):
			self.analyze_symbols(child, toplevel)

	def evaluate(self, globaldict, outputfile):
		lines = []

		def _echo(_dict, line, delim=None, newline=True):
			lines.append((line.format(**_dict), delim, newline))

		globaldict["_echo"] = _echo

		localdict = {
			"producer": lambda func: func,
			"consumer": lambda func: func,
		}

		exec(compile(self.ast, self.unit.sourcename, "exec"), globaldict, localdict)

		for name, value in localdict.items():
			if name in self.declnames:
				globaldict[name] = value

		for i, (line, delim, newline) in enumerate(lines):
			if i > 0:
				line = self.indent + line

			if delim and i < len(lines) - 1:
				line += delim

			if newline and i < len(lines) - 1:
				print(line, file=outputfile)
			else:
				print(line, end="", file=outputfile)

class Symbol:

	def __init__(self, name, unit, producer=False, consumer=False):
		assert not (producer and consumer)

		self.name = name
		self.unit = unit
		self.producer = producer
		self.consumer = consumer

class Data:

	def __init__(self):
		self.consumers = set()
		self.producers = set()

def reverse_topological_sort(vertices, edges):
	def get_out_degree(vertex):
		value = 0
		for head, tail in edges:
			if vertex == head:
				value += 1
		return value

	def get_in_edges(vertex):
		list = []
		for edge in edges:
			head, tail = edge
			if vertex == tail:
				list.append(edge)
		return list

	queue = []
	out_degrees = {}

	for vertex in vertices:
		out_degree = get_out_degree(vertex)
		if out_degree == 0:
			queue.append(vertex)
		else:
			out_degrees[vertex] = out_degree

	list = []

	while queue:
		vertex = queue.pop(0)
		list.append(vertex)

		for edge in get_in_edges(vertex):
			head, tail = edge
			out_degrees[head] -= 1
			if out_degrees[head] == 0:
				queue.append(head)

	if len(list) != len(vertices):
		raise Exception('Cyclic dependencies')

	return list

def main():
	parser = optparse.OptionParser(usage="Usage: %prog [options] FILE...")
	parser.add_option("-d", "--outputdir", metavar="DIR", default="", dest="outputdir", help="output directory")
	options, args = parser.parse_args()

	project = Project(options.outputdir)

	for filename in args:
		base = os.path.basename(filename)
		if not ("." in base and base.endswith("y")):
			print("Bad filename extension:", filename, file=sys.stdout)
			sys.exit(1)

		project.add(filename)

	project.process()

if __name__ == "__main__":
	main()
