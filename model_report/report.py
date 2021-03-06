# -*- coding: utf-8 -*-
import copy
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils.translation import ugettext_lazy as _
from django.db.models.fields import DateTimeField, DateField
from django.utils.encoding import force_unicode
from django.db.models import Q
from django import forms
from django.forms.models import fields_for_model

from model_report.utils import base_label, ReportValue, ReportRow
from model_report.highcharts import HighchartRender
from model_report.widgets import RangeField
from model_report.export_pdf import render_to_pdf


class ReportInstanceManager(object):

    _register = {}

    def __init__(self):
        self._register = {}

    def register(self, slug, rclass):
        if slug in self._register:
            raise ValueError('Slug already exists: %s' % slug)
        report = rclass()
        setattr(report, 'slug', slug)
        self._register[slug] = report

    def get_report(self, slug):
        return self._register.get(slug, None)

    def get_reports(self):
        return self._register.values()


reports = ReportInstanceManager()


_cache_class = {}


def cache_return(fun):
    def wrap(self, *args, **kwargs):
        cache_field = '%s_%s' % (self.__class__.__name__, fun.func_name)
        if cache_field in _cache_class:
            return _cache_class[cache_field]
        result = fun(self, *args, **kwargs)
        _cache_class[cache_field] = result
        return result
    return wrap


class ReportAdmin(object):

    fields = []
    model = None
    list_filter = ()
    list_order_by = ()
    list_group_by = ()
    template_name = None
    title = None
    type = 'report'
    group_totals = {}
    report_totals = {}
    override_field_values = {}
    override_field_formats = {}
    override_field_labels = {}
    list_serie_fields = ()
    chart_types = ()
    exports = ('excel', 'pdf')

    def __init__(self):
        model_fields = []
        for field in self.get_query_field_names():
            try:
                if '__' in field:  # IF field has lookup
                    pre_field = None
                    base_model = self.model
                    for field_lookup in field.split("__"):
                        if not pre_field:
                            pre_field = base_model._meta.get_field_by_name(field_lookup)[0]

                        else:
                            base_model = pre_field.rel.to
                            pre_field = base_model._meta.get_field_by_name(field_lookup)[0]
                    model_field = pre_field
                else:
                    if not 'self.' in field:
                        model_field = self.model._meta.get_field_by_name(field)[0]
                    else:
                        get_attr = lambda s: getattr(s, field.split(".")[1])
                        get_attr.verbose_name = field
                        model_field = field
            except IndexError:
                raise ValueError('The field "%s" does not exist in model "%s".' % (field, self.model._meta.module_name))
            model_fields.append([model_field, field])
        self.model_fields = model_fields

    def _get_grouper_text(self, groupby_field, value):
        try:
            model_field = [mfield for mfield, field in self.model_fields if field == groupby_field][0]
        except:
            model_field = None
        value = self.get_grouper_text(value, groupby_field, model_field)
        if value is None or unicode(value) == u'None':
            if groupby_field is None or unicode(groupby_field) == u'None':
                value = force_unicode(_('Results'))
            else:
                value = force_unicode(_('Nothing'))
        return value

    def _get_value_text(self, index, value):
        try:
            model_field = self.model_fields[index][0]
        except:
            model_field = None

        value = self.get_value_text(value, index, model_field)
        if value is None or unicode(value) == u'None':
            value = ''
        return value

    def get_grouper_text(self, value, field, model_field):
        try:
            if not isinstance(model_field, (str, unicode)):
                obj = model_field.model(**{field: value})
                if hasattr(obj, 'get_%s_display' % field):
                    value = getattr(obj, 'get_%s_display' % field)()
        except:
            pass
        return value

    def get_value_text(self, value, index, model_field):
        try:
            if not isinstance(model_field, (str, unicode)):
                obj = model_field.model(**{model_field.name: value})
                if hasattr(obj, 'get_%s_display' % model_field.name):
                    return getattr(obj, 'get_%s_display' % model_field.name)()
        except:
            pass
        return value

    def get_empty_row_asdict(self, collection, default_value=[]):
        erow = {}
        for field in collection:
            erow[field] = copy.copy(default_value)
        return dict(copy.deepcopy(erow))

    def reorder_dictrow(self, dictrow):
        return [dictrow[field_name] for field_name in self.fields]

    @cache_return
    def get_column_names(self):
        values = []
        for field, field_name in self.model_fields:
            caption = self.override_field_labels.get(field_name, base_label)(self, field)
            values.append(caption)
        return values

    @cache_return
    def get_query_field_names(self):
        values = []
        for field in self.fields:
            if not 'self.' in field:
                values.append(field.split(".")[0])
            else:
                values.append(field)
        return values

    @cache_return
    def get_query_set(self, filter_kwargs):
        qs = self.model.objects.all()
        for k, v in filter_kwargs.items():
            if not v is None and v != '':
                qs = qs.filter(Q(**{k: v}))
        return qs.distinct()

    def get_title(self):
        title = self.title or None
        if not title:
            if not self.model:
                title = _('Unnamed report')
            else:
                title = force_unicode(self.model._meta.verbose_name_plural).lower().capitalize()
        return title

    def render(self, request, extra_context={}):
        try:
            form_groupby = self.get_form_groupby(request)
            form_filter = self.get_form_filter(request)
            form_config = self.get_form_config(request)

            column_labels = self.get_column_names()
            report_rows = []
            groupby_data = None
            filter_kwargs = None
            report_anchors = []
            chart = None

            context = {
                'report': self,
                'form_groupby': form_groupby,
                'form_filter': form_filter,
                'form_config': form_config if self.type == 'chart' else None,
                'chart': chart,
                'report_anchors': report_anchors,
                'column_labels': column_labels,
                'report_rows': report_rows,
            }

            if request.GET:
                groupby_data = form_groupby.get_cleaned_data() if form_groupby else None
                filter_kwargs = form_filter.get_filter_kwargs()
                self.__dict__.update(groupby_data)
                report_rows = self.get_rows(request, groupby_data, filter_kwargs)

                for g, r in report_rows:
                    report_anchors.append(g)

                if len(report_anchors) <= 1:
                    report_anchors = []

                if self.type == 'chart':
                    config = form_config.get_config_data()
                    if config:
                        chart = self.get_chart(config, report_rows)

                if self.onlytotals:
                    for g, rows in report_rows:
                        for r in list(rows):
                            if r.is_value():
                                rows.remove(r)

                if not request.GET.get('export', None) is None:
                    if request.GET.get('export') == 'excel':
                        import csv
                        from django.http import HttpResponse

                        response = HttpResponse(mimetype='text/csv')
                        response['Content-Disposition'] = 'attachment; filename=%s.csv' % self.slug

                        writer = csv.writer(response)

                        writer.writerow([unicode(x).encode("utf-8") for x in column_labels])

                        for g, rows in report_rows:
                            for row in list(rows):
                                if row.is_value():
                                    writer.writerow([unicode(x.value).encode("utf-8") for x in row])
                                elif row.is_caption:
                                    writer.writerow([unicode(x).encode("utf-8") for x in row])
                                elif row.is_total:
                                    writer.writerow([unicode(x.value).encode("utf-8") for x in row])
                                    writer.writerow([unicode(' ').encode("utf-8") for x in row])

                        return response
                    if request.GET.get('export') == 'pdf':
                        report_anchors = None
                        setattr(self, 'is_export', True)
                        context = {
                            'report': self,
                            'column_labels': column_labels,
                            'report_rows': report_rows,
                        }
                        context.update({'pagesize': 'legal landscape'})
                        return render_to_pdf(self, 'model_report/export_pdf.html', context)

            context = {
                'report': self,
                'form_groupby': form_groupby,
                'form_filter': form_filter,
                'form_config': form_config if self.type == 'chart' else None,
                'chart': chart,
                'report_anchors': report_anchors,
                'column_labels': column_labels,
                'report_rows': report_rows,
            }

            if extra_context:
                context.update(extra_context)

            return render_to_response('model_report/report.html', context, context_instance=RequestContext(request))
        finally:
            globals()['_cache_class'] = {}

    def get_chart(self, config, report_rows):
        config['title'] = self.get_title()
        config['has_report_totals'] = not (not self.report_totals)
        config['has_group_totals'] = not (not self.group_totals)
        return HighchartRender(config).get_chart(report_rows)

    @cache_return
    def get_serie_fields(self):
        return [(index, mfield, field, caption) for index, ((mfield, field), caption) in enumerate(zip(self.model_fields, self.get_column_names())) if field in self.list_serie_fields]

    def get_form_config(self, request):

        DEFAULT_CHART_TYPES = (
            ('area', _('Area')),
            ('line', _('Line')),
            ('column', _('Columns')),
            ('pie', _('Pie')),
        )
        CHART_SERIE_OPERATOR = (
            ('', '---------'),
            ('sum', _('Sum')),
            ('len', _('Count')),
            ('avg', _('Average')),
            ('min', _('Min')),
            ('max', _('Max')),
        )

        class ConfigForm(forms.Form):

            chart_mode = forms.ChoiceField(label=_('Chart type'), choices=(), required=False)
            serie_field = forms.ChoiceField(label=_('Serie field'), choices=(), required=False)
            serie_op = forms.ChoiceField(label=_('Serie operator'), choices=CHART_SERIE_OPERATOR, required=False)

            def __init__(self, *args, **kwargs):
                super(ConfigForm, self).__init__(*args, **kwargs)
                choices = [('', '')]

                for i, (index, mfield, field, caption) in enumerate(self.serie_fields):
                    choices += (
                        (index, caption),
                    )
                self.fields['serie_field'].choices = list(choices)

                choices = [('', '')]
                for k, v in DEFAULT_CHART_TYPES:
                    if k in self.chart_types:
                        choices.append([k, v])
                self.fields['chart_mode'].choices = list(choices)

            def get_config_data(self):
                data = getattr(self, 'cleaned_data', {})
                if not data:
                    return {}
                if not data['serie_field'] or not data['chart_mode'] or not data['serie_op']:
                    return {}
                data['serie_field'] = int(data['serie_field'])
                return data

        ConfigForm.serie_fields = self.get_serie_fields()
        ConfigForm.chart_types = self.chart_types
        form = ConfigForm(data=request.GET or None)
        form.is_valid()

        return form

    @cache_return
    def get_groupby_fields(self):
        return [(mfield, field, caption) for (mfield, field), caption in zip(self.model_fields, self.get_column_names()) if field in self.list_group_by]

    @cache_return
    def get_form_groupby(self, request):
        groupby_fields = self.get_groupby_fields()

        if not groupby_fields:
            return None

        class GroupByForm(forms.Form):

            groupby = forms.ChoiceField(label=_('Group by field:'), required=False)
            onlytotals = forms.BooleanField(label=_('Show only totals'), required=False)

            def _post_clean(self):
                pass

            def __init__(self, **kwargs):
                super(GroupByForm, self).__init__(**kwargs)
                choices = [(None, '')]
                for i, (mfield, field, caption) in enumerate(self.groupby_fields):
                    choices.append((field, caption))
                self.fields['groupby'].choices = choices
                data = kwargs.get('data', {})
                if data:
                    self.fields['groupby'].initial = data.get('groupby', '')

            def get_cleaned_data(self):
                cleaned_data = getattr(self, 'cleaned_data', {})
                if 'groupby' in cleaned_data:
                    if unicode(cleaned_data['groupby']) == u'None':
                        cleaned_data['groupby'] = None
                return cleaned_data

        GroupByForm.groupby_fields = groupby_fields

        form = GroupByForm(data=request.GET or None)
        form.is_valid()

        return form

    def get_form_filter(self, request):
        form_fields = fields_for_model(self.model, [f for f in self.get_query_field_names() if f in self.list_filter])
        opts = self.model._meta
        for k, v in dict(form_fields).items():
            if v is None:
                field_name = k.split("__")[0]
                model_field = opts.get_field_by_name(field_name)[0]
                if isinstance(model_field, (DateField, DateTimeField)):
                    form_fields.pop(k)
                    form_fields[k] = RangeField(model_field.formfield)
                else:
                    field = model_field.formfield()
                    field.label = force_unicode(_(field.label))
                    form_fields[k] = field
            else:
                if isinstance(v, (forms.BooleanField)):
                    form_fields.pop(k)
                    form_fields[k] = forms.ChoiceField()
                    form_fields[k].label = v.label
                    form_fields[k].help_text = v.help_text
                    form_fields[k].choices = (
                        ('', ''),
                        (True, _('Yes')),
                        (False, _('No')),
                    )
                    setattr(form_fields[k], 'as_boolean', True)
                elif isinstance(v, (forms.DateField, forms.DateTimeField)):
                    field_name = k.split("__")[0]
                    model_field = opts.get_field_by_name(field_name)[0]
                    form_fields.pop(k)
                    form_fields[k] = RangeField(model_field.formfield)

        form_class = type('FilterFormBase', (forms.BaseForm,), {'base_fields': form_fields})

        class FilterForm(form_class):

            def _post_clean(self):
                pass

            def get_filter_kwargs(self):
                if not self.is_valid():
                    return {}
                filter_kwargs = dict(self.cleaned_data)
                for k, v in dict(filter_kwargs).items():
                    if isinstance(v, (list, tuple)):
                        if isinstance(self.fields[k], (RangeField)):
                            filter_kwargs.pop(k)
                            start_range, end_range = v
                            if start_range:
                                filter_kwargs['%s__gte' % k] = start_range
                            if end_range:
                                filter_kwargs['%s__lte' % k] = end_range
                    elif hasattr(self.fields[k], 'as_boolean'):
                        if v:
                            filter_kwargs.pop(k)
                            filter_kwargs[k] = (unicode(v) == u'True')
                return filter_kwargs

            def get_cleaned_data(self):
                return getattr(self, 'cleaned_data', {})

            def __init__(self, *args, **kwargs):
                super(FilterForm, self).__init__(*args, **kwargs)
                try:
                    data_filters = {}
                    vals = args[0]
                    for k in vals.keys():
                        if k in self.fields:
                            data_filters[k] = vals[k]
                    for name in self.fields:
                        for k, v in data_filters.items():
                            if k == name:
                                continue
                            field = self.fields[name]
                            if hasattr(field, 'queryset'):
                                qs = field.queryset
                                if k in qs.model._meta.get_all_field_names():
                                    field.queryset = qs.filter(Q(**{k: v}))
                except:
                    pass

                for field in self.fields:
                    self.fields[field].required = False
                    if hasattr(self.fields[field], 'choices'):
                        if not hasattr(self.fields[field], 'queryset'):
                            if self.fields[field].choices[0][0]:
                                self.fields[field].choices.insert(0, ('', '---------'))
                                self.fields[field].initial = ''

        form = FilterForm(data=request.GET or None)
        form.is_valid()

        return form

    def get_rows(self, request, groupby_data=None, filter_kwargs={}):

        report_rows = []

        def get_field_value(obj, field):
            if isinstance(obj, (dict)):
                return obj[field]
            left_field = field.split("__")[0]
            try:
                right_field = "__".join(field.split("__")[1:])
            except:
                right_field = ''
            if right_field:
                return get_field_value(getattr(obj, left_field), right_field)
            if hasattr(obj, 'get_%s_display' % left_field):
                attr = getattr(obj, 'get_%s_display' % field)
            else:
                attr = getattr(obj, field)
            if callable(attr):
                attr = attr()
            return attr

        from itertools import groupby
        qs = self.get_query_set(filter_kwargs)
        ffields = [f if 'self.' not in f else 'pk' for f in self.get_query_field_names()]
        obfields = list(self.list_order_by)
        if groupby_data['groupby']:
            if groupby_data['groupby'] in obfields:
                obfields.remove(groupby_data['groupby'])
            obfields.insert(0, groupby_data['groupby'])
        qs = qs.order_by(*obfields)
        qs = qs.values_list(*ffields)
        qs_list = list(qs)

        def get_with_dotvalues(resources):
            # {1: 'field.method'}
            dot_indexes = dict([(index, dot_field) for index, dot_field in enumerate(self.fields) if '.' in dot_field])
            dot_indexes_values = {}

            dot_model_fields = [(index, model_field[0]) for index, model_field in enumerate(self.model_fields) if index in dot_indexes]
            # [ 1, model_field] ]
            for index, model_field in dot_model_fields:
                model_ids = set([row[index] for row in resources])
                if isinstance(model_field, (unicode, str)) and 'self.' in model_field:
                    model_qs = self.model.objects.filter(pk__in=model_ids)
                else:
                    model_qs = model_field.rel.to.objects.filter(pk__in=model_ids)
                div = {}
                method_name = dot_indexes[index].split('.')[1]
                for obj in model_qs:
                    method_value = getattr(obj, method_name)
                    if callable(method_value):
                        method_value = method_value()
                    div[obj.pk] = method_value
                dot_indexes_values[index] = div
                del model_qs

            if dot_indexes_values:
                new_resources = []
                for index_row, old_row in enumerate(resources):
                    new_row = []
                    for index, actual_value in enumerate(old_row):
                        if index in dot_indexes_values:
                            new_value = dot_indexes_values[index][actual_value]
                        else:
                            new_value = actual_value
                        new_row.append(new_value)
                    new_resources.append(new_row)
                resources = new_resources
            return resources

        def compute_row_totals(row_config, row_values):
            total_row = self.get_empty_row_asdict(self.fields, ReportValue(' '))
            for k, v in total_row.items():
                if k in row_config:
                    fun = row_config[k]
                    value = fun(row_values[k])
                    value = ReportValue(value)
                    if k in self.override_field_values:
                        value.to_value = self.override_field_values[k]
                    if k in self.override_field_formats:
                        value.format = self.override_field_formats[k]
                    total_row[k] = value
            row = self.reorder_dictrow(total_row)
            row = ReportRow(row)
            row.is_total = True
            return row

        def compute_row_header(row_config):
            header_row = self.get_empty_row_asdict(self.fields, ReportValue(''))
            for k, fun in row_config.items():
                if hasattr(fun, 'caption'):
                    value = fun.caption
                else:
                    value = '&nbsp;'
                header_row[k] = value
            row = self.reorder_dictrow(header_row)
            row = ReportRow(row)
            row.is_caption = True
            return row

        qs_list = get_with_dotvalues(qs_list)
        if groupby_data['groupby']:
            g = groupby(qs_list, lambda x: x[ffields.index(groupby_data['groupby'])])
        else:
            g = groupby(qs_list, lambda x: None)

        row_report_totals = self.get_empty_row_asdict(self.report_totals, [])
        for grouper, resources in g:
            rows = list()
            row_group_totals = self.get_empty_row_asdict(self.group_totals, [])
            for resource in resources:
                row = ReportRow()
                if isinstance(resource, (tuple, list)):
                    for index, value in enumerate(resource):
                        if ffields[index] in self.group_totals:
                            row_group_totals[ffields[index]].append(value)
                        value = self._get_value_text(index, value)
                        value = ReportValue(value)
                        if ffields[index] in self.override_field_values:
                            value.to_value = self.override_field_values[ffields[index]]
                        if ffields[index] in self.override_field_formats:
                            value.format = self.override_field_formats[ffields[index]]
                        row.append(value)
                else:
                    for index, column in enumerate(ffields):
                        value = get_field_value(resource, column)
                        if ffields[index] in self.group_totals:
                            row_group_totals[ffields[index]].append(value)
                        value = self._get_value_text(index, value)
                        value = ReportValue(value)
                        if column in self.override_field_values:
                            value.to_value = self.override_field_values[column]
                        if column in self.override_field_formats:
                            value.format = self.override_field_formats[column]
                        row.append(value)

                rows.append(row)
            if row_group_totals:
                if groupby_data['groupby']:
                    header_group_total = compute_row_header(self.group_totals)
                    row = compute_row_totals(self.group_totals, row_group_totals)
                    rows.append(header_group_total)
                    rows.append(row)
                for k, v in row_group_totals.items():
                    row_report_totals[k].extend(v)
            grouper = self._get_grouper_text(groupby_data['groupby'], grouper)
            report_rows.append([grouper, rows])
        header_report_total = compute_row_header(self.group_totals)
        row = compute_row_totals(self.report_totals, row_report_totals)
        header_report_total.is_report_totals = True
        row.is_report_totals = True
        report_rows.append([_('Totals'), [header_report_total, row]])

        return report_rows
