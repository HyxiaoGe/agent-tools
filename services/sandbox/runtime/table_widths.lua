--[[
convert-doc 出 Word 时给 pandoc 用的过滤器：给每张表格定各列的宽度（占版心的比例）。

不定的话，pandoc 把各列写成一样宽，留给打开文件的程序自己按内容调。Word 会调；LibreOffice 不调
（2026-11-12 实测：每列一样宽，数字列空着一大半，长说明被挤成一竖条）。这里按内容算好，哪个程序打开都一个样：
- 每列先给够最窄宽度：这一列里最长的一串断不开的字（数字、单词；中文每个字后面都能断行，最窄一个字）。
  短的表头整个不折行，也算进最窄宽度。
- 剩下的宽度按各列“一行排完要多宽”来分，一列最多算 28 个字宽，免得一列长说明把别的列挤没了。
- 最窄宽度加起来就比版心宽：没法都满足，按最窄宽度的比例分。

宽度是估的，不是量的：中文、全角符号算一个字宽，其余算 0.55 个，以表格的字号为单位。
有合并格子的表格不动。出 PDF 走的是另一条路（convert_doc.py 的 size_tables 和 document.css），规矩一样。
]]

-- 下面三个数和 make_reference_docx.py 里的版面对应：A4 减左右各 2.6 厘米是 448 磅，表格的字 9.5 磅
local TEXT_EM = 47.0 -- 版心的宽度
local PAD_EM = 1.3 -- 一格左右的留白（各 5 磅），再多留一点：宽度是估的
local CAP_EM = 28.0 -- 一列“一行排完”最多算这么宽
local HEADER_KEEP_EM = 8.0 -- 表头不超过这么宽就不折行
local LONG_TOKEN_CHARS = 20 -- 断不开的一串到这么长，就不指望给它留够宽度（由打开文件的程序硬断）
local FLOOR_EM = 2.0 -- 一列至少这么宽
local FIRST_COLUMN_EM = 4.6 -- 第一列多半是行名（“开发语言”“毛利率”），内容有这么宽就别折行

-- 格子里的字。公式换成短一些的占位：它的 TeX 源码比排出来的样子长得多
local function text_of(cell)
  local blocks = pandoc.Blocks(cell.contents):walk({
    Math = function(el)
      return pandoc.Str(string.rep("x", math.max(2, #el.text // 3)))
    end,
  })
  return pandoc.utils.stringify(blocks)
end

-- 一段字：一行排完多宽、最窄多宽
local function measure(text)
  local whole, narrowest, run, run_chars = 0, 0, 0, 0
  local function close()
    if run_chars < LONG_TOKEN_CHARS and run > narrowest then
      narrowest = run
    end
    run, run_chars = 0, 0
  end
  for _, code in utf8.codes(text) do
    if code == 32 or code == 9 or code == 10 then
      whole = whole + 0.3
      close()
    elseif code >= 0x2E80 then
      whole = whole + 1.0
      close()
      narrowest = math.max(narrowest, 1.0)
    else
      whole = whole + 0.55
      run, run_chars = run + 0.55, run_chars + 1
    end
  end
  close()
  return whole, narrowest
end

function Table(tbl)
  local count = #tbl.colspecs
  local whole, narrowest = {}, {}
  for i = 1, count do
    whole[i], narrowest[i] = FLOOR_EM, FLOOR_EM
  end
  local merged = false

  local function visit(rows, header)
    for _, row in ipairs(rows) do
      for i, cell in ipairs(row.cells) do
        if cell.col_span > 1 or cell.row_span > 1 then
          merged = true
        end
        if i <= count then
          local w, n = measure(text_of(cell))
          if header then
            w = w * 1.05 -- 表头是粗体，略宽
            if w <= HEADER_KEEP_EM then
              n = w
            end
          end
          whole[i] = math.max(whole[i], w)
          narrowest[i] = math.max(narrowest[i], n)
        end
      end
    end
  end

  visit(tbl.head.rows, true)
  for _, body in ipairs(tbl.bodies) do
    visit(body.head, true)
    visit(body.body, false)
  end
  visit(tbl.foot.rows, false)
  if merged or count == 0 then
    return nil
  end

  narrowest[1] = math.max(narrowest[1], math.min(whole[1], FIRST_COLUMN_EM))
  local room = TEXT_EM - PAD_EM * count
  local sum_whole, sum_narrowest = 0, 0
  for i = 1, count do
    whole[i] = math.max(math.min(whole[i], CAP_EM), narrowest[i])
    sum_whole = sum_whole + whole[i]
    sum_narrowest = sum_narrowest + narrowest[i]
  end

  local widths, total = {}, 0
  for i = 1, count do
    local width
    if sum_narrowest >= room or sum_whole <= sum_narrowest then
      width = narrowest[i]
    elseif sum_whole <= room then
      width = whole[i]
    else
      width = narrowest[i] + (room - sum_narrowest) * (whole[i] - narrowest[i]) / (sum_whole - sum_narrowest)
    end
    widths[i] = width + PAD_EM
    total = total + widths[i]
  end

  local specs = tbl.colspecs
  for i = 1, count do
    specs[i] = { specs[i][1], widths[i] / total }
  end
  tbl.colspecs = specs
  return tbl
end
