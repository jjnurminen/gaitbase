# -*- coding: utf-8 -*-
"""
XXX: these should go into separate tech notes


Program for input and reporting of ROM (range of motion), strength and other
measurements.

Instead of saving to JSON, this version works directly with a SQL database.

Notes:

-We don't do any type conversion before SQL writes. For a given variable, the
type of the value might change from one write to another (i.e. from string "Ei
mitattu" to float 5.0). This works with SQLite, since it uses dynamic typing.
For any other database engine, it will be necessary to convert the values on
read/write, so that static types are maintained.

-The SQL database uses NULL as a marker for values that are completely missing
(e.g. due to schema changes). These can only be read correctly by disabling
PyQt type autoconversion, reading data as QVariants and using isNull() to detect
NULLs. Hence, we do all SQL reads with the autoconversion disabled.

-The sqlite3 database writes require an EXCLUSIVE lock while they are carried
out. This means that all SHARED locks must be released before writes can take
place. For example, QtSql may hold SHARED locks indefinitely in some
circumstances (e.g. lazy reads), preventing writes (at least writes from different
processes). These problems must be worked around, i.e. locks released as soon as
possible after reads are completed.

-To write numerical variables (such as angles) we use the NUMERIC affinity. A
side effect of the above is that all float values without a decimal part (e.g. 5.0)
will be written into the database as integers (5). In this respect, the saved
data differs from the original JSON format, which preservers float values.

-For now, new ROMs also will be dumped into JSON files (just in case).

-There's a custom widget (CheckDegSpinBox). To properly see it in Qt Designer,
the plugin file checkspinbox_plugin.py should be made available. Before running
Qt Designer, do 'export PYQTDESIGNERPATH=path' where path is the path to the
plugin.

-Input widget naming convention: first 2-3 chars indicate widget type
 (mandatory), next word indicate variable category or page where widget
 resides the rest indicates the variable. E.g. 'lnTiedotNimi'
 
 -specially named widgets are automatically recognized as data inputs:
 widgets whose names start with one of 'ln', 'sp', 'csb', 'xb', or 'cmt'
 
-data inputs are updated into an internal dict whenever any value changes

-dict keys are taken automatically from widget names by removing first 2-3
 chars (widget type)

-for certain inputs, there is a special value indicating "not measured". For
text inputs, this is just the empty string. For comboboxes, there is a
distinct "not measured" value. For spinboxes, Qt supports a special value text
that will be shown whenever the widget is at its minimum value. When values are
read from spinboxes, the minimum value is automatically converted (by us) to
a minimum value string.

-magic mechanism for weight normalized data: widgets can have names ending
with 'NormUn' which creates a weight unnormalized value. The
corresponding widget name with UnNorm replaced by Norm (which must exist)
is then automatically updated whenever either weight or the unnormalized value
changes

@author: Jussi (jnu@iki.fi)
"""

import sys
import json
import datetime
from PyQt5.QtSql import QSqlQuery
import sip
from PyQt5 import uic, QtCore, QtWidgets
import webbrowser
import logging
from pkg_resources import resource_filename

# DEBUG
#import debugpy
#debugpy.debug_this_thread()


from .constants import Constants, Finnish
from .config import cfg
from .widgets import (
    MyLineEdit,
    DegLineEdit,
    CheckDegSpinBox,
    message_dialog,
)
from . import reporter

logger = logging.getLogger(__name__)


def debug_print(msg):
    print(msg)
    sys.stdout.flush()


def pyqt_disable_autoconv(func):
    """Disable Qt type autoconversion for a function.

    PyQt functions decorated with this will return QVariants from many Qt
    functions, instead of native Python types. The QVariants then need to be
    converted manually, often using value().
    """

    def wrapper(*args, **kwargs):
        sip.enableautoconversion(QtCore.QVariant, False)
        res = func(*args, **kwargs)
        sip.enableautoconversion(QtCore.QVariant, True)
        return res

    return wrapper


class EntryApp(QtWidgets.QMainWindow):
    """Data entry window"""

    closing = QtCore.pyqtSignal(object)

    def __init__(self, database, rom_id, newly_created=None):
        super().__init__()
        # load user interface made with Qt Designer
        uifile = resource_filename('gaitbase', 'tabbed_design_sql.ui')
        uic.loadUi(uifile, self)
        """
        Explicit tab order needs to be set because Qt does not handle focus correctly
        for custom (compound) widgets (QTBUG-10907). For custom widgets, the focus proxy
        needs to be explicitly inserted into focus chain. The code in fix_taborder.py can
        be generated by running:
        pyuic5.bat tabbed_design.ui | grep TabOrder | sed "s/csb[a-zA-Z0-9]*/&.focusProxy()/g" | sed "s/    MainWindow/self/g" >fix_taborder.py
        (replaces csb* widget names with csb*.focusProxy())
        It should be regenerated whenever new widgets are introduced that are part of the focus chain.
        Before that, define focus chain in Qt Designer.
        """
        # taborder_file = resource_filename('gaitbase', 'fix_taborder.py')
        # exec(open(taborder_file, "rb").read())
        self.confirm_close = True  # used to implement force close
        self.init_widgets()
        self.data = {}
        self.read_forms()  # read default data from widgets
        self.data_empty = self.data.copy()
        # whether to update internal dict of variables on input changes
        self.update_dict = True
        self.text_template = resource_filename('gaitbase', Constants.text_template)
        self.xls_template = resource_filename('gaitbase', Constants.xls_template)
        self.database = database
        self.rom_id = rom_id
        self.newly_created = newly_created
        if database is not None:
            # the read only fields are uneditable, they reside in the patients table
            self.init_readonly_fields()
        if newly_created:
            # automatically set the date field
            datestr = datetime.datetime.now().strftime('%d.%m.%Y')
            self.lnTiedotPvm.setText(datestr)
            # for a newly created entry, initialize the database row w/ default values
            self.values_changed(self.lnTiedotPvm)
        else:
            # for existing entry, read values from database
            if database is not None:
                self._read_data()
        self.BACKUP_NEW_ROMS = True  # also dump new ROMs into JSON files

    def force_close(self):
        """Force close without confirmation"""
        self.confirm_close = False
        self.close()

    def db_failure(self, query, fatal=True):
        """Handle database failures"""
        err = query.lastError().databaseText()
        msg = f'Got a database error: "{err}"'
        msg += '\nIn case of locking errors, close all other applications '
        msg += 'that may be using the database, and try again.'
        if fatal:
            raise RuntimeError(msg)
        else:
            message_dialog(msg)

    @pyqt_disable_autoconv
    def select(self, vars):
        """Perform select on current ROM row to get data.
        vars is a list of desired variables.
        Will return a list of QVariant objects.
        Use QVariant().value() to get the values.
        """
        q = QSqlQuery(self.database)
        varlist = ','.join(vars)
        q.prepare(f'SELECT {varlist} FROM roms WHERE rom_id = :rom_id')
        q.bindValue(':rom_id', self.rom_id)
        if not q.exec() or not q.first():
            self.db_failure(q, fatal=True)
        results = tuple(q.value(k) for k in range(len(vars)))
        return results

    def update_rom(self, vars, values):
        """Update ROM row with a list of fields and corresponding values"""
        if not len(vars) == len(values):
            raise ValueError('Arguments need to be of equal length')
        q = QSqlQuery(self.database)
        varlist = ','.join(f'{var} = :{var}' for var in vars)
        q.prepare(f'UPDATE roms SET {varlist} WHERE rom_id = :rom_id')
        q.bindValue(':rom_id', self.rom_id)
        # XXX: note that we don't do any type conversion here. For a given
        # variable, the type of the value might change from one write to another
        # (i.e. from string "Ei mitattu" to float 5.0). This works with SQLite,
        # since it uses dynamic typing. For any other database engine, it will
        # be necessary to convert the values on read/write, so that static types
        # are maintained.
        for var, val in zip(vars, values):
            q.bindValue(f':{var}', val)
        if not q.exec():
            # it's possible that locking failures may occur, so make them non-fatal
            self.db_failure(q, fatal=False)

    def init_readonly_fields(self):
        """Fill the read-only patient info widgets"""
        patient_id = self.select(['patient_id'])[0].value()
        q = QSqlQuery(self.database)
        vars = ['firstname', 'lastname', 'ssn', 'patient_code', 'diagnosis']
        varlist = ','.join(vars)
        q.prepare(f'SELECT {varlist} FROM patients WHERE patient_id = :patient_id')
        q.bindValue(':patient_id', patient_id)
        if not q.exec() or not q.first():
            self.db_failure(fatal=True)
        for k, var in enumerate(vars):
            val = q.value(k)
            widget_name = 'rdonly_' + var
            self.__dict__[widget_name].setText(val)
            self.__dict__[widget_name].setEnabled(False)

    def get_patient_id_data(self):
        """Get patient id data from the read-only fields as a dict.
        In the SQL version, the patient data is not part of ROM measurements anymore, instead
        residing in the patients table.
        The returned keys are identical to the old (standalone) version.
        Mostly for purposes of reporting, which expects the ID data to be available.
        """
        return {
            'TiedotID': self.rdonly_patient_code.text(),
            'TiedotNimi': f'{self.rdonly_firstname.text()} {self.rdonly_lastname.text()}',
            'TiedotHetu': self.rdonly_ssn.text(),
            'TiedotDiag': self.rdonly_diagnosis.text(),
        }

    def eventFilter(self, source, event):
        """Captures the FocusOut event for text widgets.
        
        The idea is to perform data updates when widget focus is lost.
        """
        if event.type() == QtCore.QEvent.FocusOut:
            self.values_changed(source)
        return super().eventFilter(source, event)

    def init_widgets(self):
        """Make a dict of our input widgets and install some callbacks and
        convenience methods etc."""
        self.input_widgets = {}

        def spinbox_getval(w):
            """Return spinbox value"""
            return w.no_value_text if w.value() == w.minimum() else w.value()

        def spinbox_setval(w, val):
            """Set spinbox value"""
            val = w.minimum() if val == w.no_value_text else val
            w.setValue(val)

        def checkbox_getval(w):
            """Return yestext or notext for checkbox enabled/disabled,
            respectively."""
            val = int(w.checkState())
            if val == 0:
                return w.no_text
            elif val == 2:
                return w.yes_text
            else:
                raise RuntimeError(
                    f'Unexpected checkbox value: {val} for {w.objectName()}'
                )

        def checkbox_setval(w, val):
            """Set checkbox value to enabled for val == yestext and
            disabled for val == notext"""
            if val == w.yes_text:
                w.setCheckState(2)
            elif val == w.no_text:
                w.setCheckState(0)
            else:
                raise RuntimeError(
                    f'Unexpected checkbox entry value: {val} for {w.objectName()}'
                )

        def combobox_getval(w):
            """Get combobox current choice as text"""
            return w.currentText()

        def combobox_setval(w, val):
            """Set combobox value according to val (unicode) (must be one of
            the combobox items)"""
            idx = w.findText(val)
            if idx >= 0:
                w.setCurrentIndex(idx)
            else:
                raise ValueError(f'Tried to set combobox to invalid value {val}')

        def keyPressEvent_resetOnEsc(obj, event):
            """Special event handler for spinboxes. Resets value (sets it
            to minimum) when Esc is pressed."""
            if event.key() == QtCore.Qt.Key_Escape:
                obj.setValue(obj.minimum())
            else:
                # delegate the event to the overridden superclass handler
                super(obj.__class__, obj).keyPressEvent(event)

        def isint(x):
            """Test for integer"""
            try:
                int(x)
                return True
            except ValueError:
                return False

        # Change lineEdit to custom one for spinboxes. This cannot be done in
        # the main widget loop below, because the old QLineEdits get destroyed in
        # the process (by Qt) and the loop then segfaults while trying to
        # dereference them (the loop collects all QLineEdits at the start).
        # Also install special keypress event handler. """
        for w in self.findChildren((QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            wname = w.objectName()
            if wname[:2] == 'sp':
                w.setLineEdit(MyLineEdit())
                w.keyPressEvent = lambda event, w=w: keyPressEvent_resetOnEsc(w, event)

        # CheckDegSpinBoxes get a special LineEdit that catches space
        # and mouse press events

        for w in self.findChildren(CheckDegSpinBox):
            w.degSpinBox.setLineEdit(DegLineEdit())

        allwidgets = self.findChildren(QtWidgets.QWidget)

        def _weight_normalize(w):
            """Auto calculate callback for weight normalized widgets"""
            val, weight = (w.getVal() for w in w._autoinputs)
            noval = Constants.spinbox_novalue_text
            w.setVal(noval if val == noval or weight == noval else val / weight)

        # Autowidgets are special widgets with automatically computed values.
        # They must have an ._autocalculate() method which updates the widget
        # and ._autoinputs list which lists the necessary input widgets.
        self.autowidgets = list()
        weight_widget = self.spAntropPaino
        for w in allwidgets:
            wname = w.objectName()
            # handle the 'magic' autowidgets with weight normalized data
            if wname[-4:] == 'Norm':
                self.autowidgets.append(w)
                # corresponding unnormalized widget
                wname_unnorm = wname.replace('Norm', 'NormUn')
                w_unnorm = self.__dict__[wname_unnorm]
                w._autoinputs = [w_unnorm, weight_widget]
                w._autocalculate = lambda w=w: _weight_normalize(w)

        # autowidget values cannot be directly modified
        for w in self.autowidgets:
            w.setEnabled(False)

        # set various widget convenience methods/properties
        # input widgets are specially named and will be automatically
        # collected into a dict
        for w in allwidgets:
            wname = w.objectName()
            wsave = True
            # w.unit returns the unit for each input (may change dynamically)
            w.unit = lambda: ''
            if wname[:2] == 'sp':  # spinbox or doublespinbox
                # -lambdas need default arguments because of late binding
                # -lambda expression needs to consume unused 'new value' arg,
                # therefore two parameters (except for QTextEdit...)
                w.valueChanged.connect(lambda x, w=w: self.values_changed(w))
                w.no_value_text = Constants.spinbox_novalue_text
                w.setVal = lambda val, w=w: spinbox_setval(w, val)
                w.getVal = lambda w=w: spinbox_getval(w)
                w.unit = lambda w=w: w.suffix() if isint(w.getVal()) else ''
            elif wname[:2] == 'ln':  # lineedit
                # for text editors, do not perform data updates on every value change...
                # w.textChanged.connect(lambda x, w=w: self.values_changed(w))
                w.setVal = w.setText
                w.getVal = lambda w=w: w.text().strip()
                # instead, update values when focus is lost (editing completed)
                w.installEventFilter(self)
            elif wname[:2] == 'cb':  # combobox
                w.currentIndexChanged.connect(lambda x, w=w: self.values_changed(w))
                w.setVal = lambda val, w=w: combobox_setval(w, val)
                w.getVal = lambda w=w: combobox_getval(w)
            elif wname[:3] == 'cmt':  # comment text field
                # for text editors, do not perform data updates on every value change...
                # w.textChanged.connect(lambda w=w: self.values_changed(w))
                w.setVal = w.setPlainText
                w.getVal = lambda w=w: w.toPlainText().strip()
                # instead, update values when focus is lost (editing completed)
                w.installEventFilter(self)
            elif wname[:2] == 'xb':  # checkbox
                w.stateChanged.connect(lambda x, w=w: self.values_changed(w))
                w.yes_text = Constants.checkbox_yestext
                w.no_text = Constants.checkbox_notext
                w.setVal = lambda val, w=w: checkbox_setval(w, val)
                w.getVal = lambda w=w: checkbox_getval(w)
            elif wname[:3] == 'csb':  # checkdegspinbox
                w.valueChanged.connect(lambda w=w: self.values_changed(w))
                w.getVal = w.value
                w.setVal = w.setValue
                w.unit = lambda w=w: w.getSuffix() if isint(w.getVal()) else ''
            else:
                wsave = False
            if wsave:
                self.input_widgets[wname] = w
                # TODO: specify whether input value is 'mandatory' or not
                w.important = False

        # slot called on tab change
        self.maintab.currentChanged.connect(self.page_change)

        """ First widget of each page. This is used to do focus/selectall on
        the 1st widget on page change so that data can be entered immediately.
        Only needed for spinbox / lineedit widgets. """
        self.firstwidget = dict()
        # TODO: check/fix
        self.firstwidget[self.tabTiedot] = self.rdonly_firstname
        self.firstwidget[self.tabKysely] = self.lnKyselyPaivittainenMatka
        self.firstwidget[self.tabAntrop] = self.spAntropAlaraajaOik
        self.firstwidget[self.tabLonkka] = self.csbLonkkaFleksioOik
        self.firstwidget[self.tabNilkka] = self.csbNilkkaSoleusCatchOik
        self.firstwidget[self.tabPolvi] = self.csbPolviEkstensioVapOik
        self.firstwidget[self.tabIsokin] = self.spIsokinPolviEkstensioOik
        self.firstwidget[self.tabVirheas] = self.spVirheasAnteversioOik
        self.firstwidget[self.tabTasap] = self.spTasapOik
        self.total_widgets = len(self.input_widgets)

        self.statusbar.showMessage(Finnish.ready.format(n=self.total_widgets))

        """ Set up widget -> varname translation dict. Variable names
        are derived by removing 2-3 leading characters (indicating widget type)
        from widget names (except for comment box variables cmt* which are
        identical with widget names).
        """
        self.widget_to_var = dict()
        for wname in self.input_widgets:
            if wname[:3] == 'cmt':
                varname = wname
            elif wname[:3] == 'csb':  # custom widget
                varname = wname[3:]
            else:
                varname = wname[2:]
            self.widget_to_var[wname] = varname

        # try to increase font size
        self.setStyleSheet('QWidget { font-size: %dpt;}' % cfg.visual.fontsize)

        # FIXME: make sure we always start on 1st tab

    @property
    def units(self):
        """Return dict indicating the units for each variable. This may change
        dynamically as the unit may be set to '' for special values."""
        return {
            self.widget_to_var[wname]: self.input_widgets[wname].unit()
            for wname in self.input_widgets
        }

    @property
    def vars_default(self):
        """Return a list of variables that are at their default (unmodified)
        state."""
        return [key for key in self.data if self.data[key] == self.data_empty[key]]

    def do_close(self, event):
        """The actual closing ritual"""
        # XXX: we may want to undo the database entry, if no values were entered
        # if self.n_modified() == 0:
        # XXX: if ROM was newly created, we also create JSON for backup purposes
        # this is for the "beta phase"  only
        if self.BACKUP_NEW_ROMS and self.newly_created:
            # XXX: this will overwrite existing files, but they should be uniquely named due to
            # timestamp in the filename
            fn = self._compose_json_filename()
            try:
                self.save_file(fn)
            except IOError:  # ignore errors for now
                pass
        self.closing.emit(self.rom_id)
        event.accept()

    def closeEvent(self, event):
        """Confirm and close application."""
        # Since some widgets update only when losing focus, we want to make sure
        # they lose focus before closing the app, so that data is updated.
        self.setFocus()
        if not self.confirm_close:  # force close
            self.do_close(event)
        else:  # closing via ui
            status_ok, msg = self._validate_outputs()
            if status_ok:
                self.do_close(event)
            else:
                message_dialog(msg)
                event.ignore()

    def _validate_date(self, datestr):
        """Validate date"""
        try:
            datetime.datetime.strptime(datestr, '%d.%m.%Y')
            return True
        except ValueError:
            return False

    def _validate_outputs(self):
        """Validate inputs before closing"""
        date = self.data['TiedotPvm']
        if not self._validate_date(date):
            return False, 'Päivämäärän täytyy olla oikea ja muodossa pp.kk.vvvv'
        else:
            return True, ''

    def values_changed(self, w):
        """Called whenever value of a widget (w) changes.

        This does several things, most importantly updates the database.
        """
        # find autowidgets that depend on w and update them
        autowidgets_this = [
            widget for widget in self.autowidgets if w in widget._autoinputs
        ]
        for widget in autowidgets_this:
            widget._autocalculate()
        if self.update_dict:
            # update internal data dict
            wname = w.objectName()
            varname = self.widget_to_var[wname]
            newval = w.getVal()
            self.data[varname] = newval
            # perform the corresponding SQL update
            self.update_rom([varname], [newval])

    def keyerror_dialog(self, origkeys, newkeys):
        """Report missing / unknown keys to user."""
        cmnkeys = origkeys.intersection(newkeys)
        extra_in_new = newkeys - cmnkeys
        not_in_new = origkeys - cmnkeys
        li = list()
        if extra_in_new:
            # keys in data but not in UI - data lost
            li.append(Finnish.keys_extra.format(keys=', '.join(extra_in_new)))
        if not_in_new:
            # keys in UI but not in data. this is acceptable
            li.append(Finnish.keys_not_found.format(keys=', '.join(not_in_new)))
        # only show the dialog if data was lost (not for missing values)
        if extra_in_new:
            message_dialog(''.join(li))

    @property
    def data_with_units(self):
        """Append units to values"""
        return {key: f'{self.data[key]}{self.units[key]}' for key in self.data}

    def _read_data(self):
        """Read input data from database"""
        vars = list(self.data.keys())
        # get data as QVariants, and ignore NULL ones (which correspond to missing data in database)
        qvals = self.select(vars)
        record_di = {
            var: qval.value() for var, qval in zip(vars, qvals) if not qval.isNull()
        }
        self.data = self.data_empty | record_di
        self.restore_forms()

    def _compose_json_filename(self):
        """Make up a JSON filename"""
        pdata = self.get_patient_id_data() | self.data
        fn = pdata['TiedotID']
        fn += '_'
        fn += ''.join(reversed(pdata['TiedotNimi'].split()))
        fn += '_'
        fn += datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
        fn += '.json'
        return Constants.json_backup_path / fn

    def save_file(self, fn):
        """Save data into given file in utf-8 encoding"""
        # ID data is not updated from widgets in the SQL version, so get it separately
        rdata = self.data | self.get_patient_id_data()
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(json.dumps(rdata, ensure_ascii=False, indent=True, sort_keys=True))

    def make_txt_report(self, template, include_units=True):
        """Create text report from current data"""
        # uncomment to respond to template changes while running
        # importlib.reload(reporter)
        data = self.data_with_units if include_units else self.data
        # ID data is not updated from widgets in the SQL version, so get it separately
        rdata = data | self.get_patient_id_data()
        rep = reporter.Report(rdata, self.vars_default)
        return rep.make_text_report(template)

    def make_excel_report(self):
        """Create Excel report from current data"""
        # ID data is not updated from widgets in the SQL version, so get it separately
        rdata = self.data | self.get_patient_id_data()
        rep = reporter.Report(rdata, self.vars_default)
        return rep.make_excel_report(self.xls_template)

    def n_modified(self):
        """Count modified values."""
        return len([x for x in self.data if self.data[x] != self.data_empty[x]])

    def page_change(self):
        """Callback for tab change"""
        newpage = self.maintab.currentWidget()
        # focus / selectAll on 1st widget of new tab
        if newpage in self.firstwidget:
            widget = self.firstwidget[newpage]
            if widget.isEnabled():
                widget.selectAll()
                widget.setFocus()

    def restore_forms(self):
        """Restore widget input values from self.data. Need to disable widget
        callbacks and automatic data saving while programmatic updating of
        widgets is taking place."""
        self.save_to_tmp = False
        self.update_dict = False
        for wname in self.input_widgets:
            self.input_widgets[wname].setVal(self.data[self.widget_to_var[wname]])
        self.save_to_tmp = True
        self.update_dict = True

    def read_forms(self):
        """Read self.data from widget inputs. Usually not needed, since
        it's updated automatically."""
        for wname in self.input_widgets:
            var = self.widget_to_var[wname]
            self.data[var] = self.input_widgets[wname].getVal()
